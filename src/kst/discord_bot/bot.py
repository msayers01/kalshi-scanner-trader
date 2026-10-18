"""Discord bot: phone control panel + notifications.

* Persistent control panel (buttons survive restarts): Pause · Resume · KILL · Kill+Flatten (2-step
  confirm) · Status · P&L · Autotrade toggle. `/panel` posts it.
* Slash commands: /status /pnl /positions /scan /pause /resume /kill /flatten /autotrade /limits /setlimit
* Only `discord.allowed_user_ids` may use controls (checked on every interaction; replies are ephemeral).
* Notifier: fills (batched), engine state changes, risk alerts, settlements, daily report — rate limited.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from kst.api import serialize as ser
from kst.core.events import (
    Alert,
    DailyReport,
    EngineStateChanged,
    Event,
    FillReceived,
    MarketSettled,
    RiskLimitsChanged,
)
from kst.core.fixedpoint import MICROS_PER_DOLLAR
from kst.core.logging import get_logger
from kst.core.types import Actor
from kst.discord_bot import formatting as fmt

if TYPE_CHECKING:
    from kst.app import Application

log = get_logger(__name__)


class ConfirmFlatten(discord.ui.View):
    def __init__(self, bot: KstBot) -> None:
        super().__init__(timeout=30)
        self.bot = bot

    @discord.ui.button(label="Yes — KILL & FLATTEN everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if not await self.bot.authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        res = await self.bot.app.control.kill(
            Actor.DISCORD, "discord kill+flatten", flatten=True, who=str(interaction.user.id)
        )
        flat = res.get("flatten", {})
        lines = [
            f"{r['ticker']}: {'ok' if r['ok'] else 'FAILED'} filled {r.get('filled', 0)} rem {r.get('remaining', 0)} {r.get('reason', '')}"
            for r in flat.get("results", [])
        ]
        await interaction.followup.send(
            "🛑 KILLED + flatten:\n" + ("\n".join(lines) or "no positions"), ephemeral=True
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


class ControlPanel(discord.ui.View):
    """Persistent view: custom_ids are stable so buttons keep working after a restart."""

    def __init__(self, bot: KstBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="kst:pause", row=0
    )
    async def pause(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            res = await self.bot.app.control.pause(
                Actor.DISCORD, "discord button", who=str(interaction.user.id)
            )
            await interaction.followup.send(
                f"⏸️ Paused (cancelled {res['cancelled_entries']} entry orders). Exits still managed.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Resume", emoji="▶️", style=discord.ButtonStyle.success, custom_id="kst:resume", row=0
    )
    async def resume(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if await self.bot.authorized(interaction):
            await self.bot.app.control.resume(Actor.DISCORD, who=str(interaction.user.id))
            await interaction.response.send_message("▶️ Running.", ephemeral=True)

    @discord.ui.button(
        label="KILL", emoji="🛑", style=discord.ButtonStyle.danger, custom_id="kst:kill", row=0
    )
    async def kill(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            res = await self.bot.app.control.kill(
                Actor.DISCORD, "discord kill button", who=str(interaction.user.id)
            )
            await interaction.followup.send(
                f"🛑 KILLED. {res['cancelled']} orders cancelled. Positions untouched — use Kill+Flatten to exit.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Kill + Flatten", emoji="💥", style=discord.ButtonStyle.danger, custom_id="kst:flatten", row=0
    )
    async def flatten(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if await self.bot.authorized(interaction):
            n = len(ser.positions(self.bot.app))
            await interaction.response.send_message(
                f"Exit **all {n} positions** at market (≤ slippage band) and kill the engine?",
                view=ConfirmFlatten(self.bot),
                ephemeral=True,
            )

    @discord.ui.button(
        label="Status", emoji="📊", style=discord.ButtonStyle.primary, custom_id="kst:status", row=1
    )
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.send_message(embed=self.bot.status_embed(), ephemeral=True)

    @discord.ui.button(label="P&L", emoji="💰", style=discord.ButtonStyle.primary, custom_id="kst:pnl", row=1)
    async def pnl(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.send_message(
                embed=fmt.pnl_embed(self.bot.app.portfolio.summary().to_dict()), ephemeral=True
            )

    @discord.ui.button(
        label="Positions", emoji="📋", style=discord.ButtonStyle.primary, custom_id="kst:positions", row=1
    )
    async def positions(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if await self.bot.authorized(interaction):
            await interaction.response.send_message(
                embed=fmt.positions_embed(ser.positions(self.bot.app)), ephemeral=True
            )

    @discord.ui.button(
        label="Autotrade on/off",
        emoji="🤖",
        style=discord.ButtonStyle.secondary,
        custom_id="kst:autotrade",
        row=1,
    )
    async def autotrade(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if await self.bot.authorized(interaction):
            on = not self.bot.app.control.autotrade
            await self.bot.app.control.set_autotrade(on, Actor.DISCORD, who=str(interaction.user.id))
            await interaction.response.send_message(f"🤖 Autotrade {'ON' if on else 'OFF'}.", ephemeral=True)


class KstBot(commands.Bot):
    def __init__(self, app: Application) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!kst ", intents=intents)
        self.app = app
        self.cfg = app.settings.discord
        self.allowed = set(self.cfg.allowed_user_ids)
        self._fill_buf: list[dict[str, Any]] = []
        self._register_commands()

    # ------------------------------------------------------------------ auth / helpers

    async def authorized(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.allowed:
            return True
        log.warning("discord_unauthorized", user=interaction.user.id)
        with contextlib.suppress(Exception):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
        return False

    def status_embed(self) -> discord.Embed:
        return fmt.status_embed(self.app.control.status(), self.app.gate.status())

    async def channel(self) -> discord.abc.Messageable | None:
        if not self.cfg.channel_id:
            return None
        ch = self.get_channel(self.cfg.channel_id)
        if ch is None:
            with contextlib.suppress(Exception):
                ch = await self.fetch_channel(self.cfg.channel_id)
        return ch if isinstance(ch, discord.abc.Messageable) else None

    # ------------------------------------------------------------------ lifecycle

    async def setup_hook(self) -> None:
        self.add_view(ControlPanel(self))
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self._notifier_task = self.loop.create_task(self._notifier())  # keep a ref: tasks can be GC'd

    async def on_ready(self) -> None:
        log.info("discord_ready", user=str(self.user), guilds=len(self.guilds))
        ch = await self.channel()
        if ch is not None:
            await ch.send(embed=self.status_embed(), view=ControlPanel(self))

    # ------------------------------------------------------------------ slash commands

    def _register_commands(self) -> None:
        tree = self.tree
        app = self.app

        @tree.command(name="status", description="Engine state, equity, today's P&L")
        async def status(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                await interaction.response.send_message(embed=self.status_embed(), ephemeral=True)

        @tree.command(name="pnl", description="Today's P&L summary")
        async def pnl(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                await interaction.response.send_message(
                    embed=fmt.pnl_embed(app.portfolio.summary().to_dict()), ephemeral=True
                )

        @tree.command(name="positions", description="Open positions")
        async def positions(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                await interaction.response.send_message(
                    embed=fmt.positions_embed(ser.positions(app)), ephemeral=True
                )

        @tree.command(name="scan", description="Top scanner rows")
        @app_commands.describe(n="rows (max 20)")
        async def scan(interaction: discord.Interaction, n: int = 10) -> None:
            if await self.authorized(interaction):
                rows = ser.scan(app, max(1, min(n, 20)))["rows"]
                await interaction.response.send_message(embed=fmt.scan_embed(rows), ephemeral=True)

        @tree.command(name="pause", description="Stop new entries (exits continue)")
        async def pause(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                # Cancelling entries can outlast Discord's 3 s reply window: acknowledge first.
                await interaction.response.defer(ephemeral=True, thinking=True)
                await app.control.pause(Actor.DISCORD, "slash /pause", who=str(interaction.user.id))
                await interaction.followup.send("⏸️ Paused.", ephemeral=True)

        @tree.command(name="resume", description="Resume trading")
        async def resume(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                await app.control.resume(Actor.DISCORD, who=str(interaction.user.id))
                await interaction.response.send_message("▶️ Running.", ephemeral=True)

        @tree.command(name="kill", description="KILL: cancel all orders and stop trading")
        async def kill(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                await interaction.response.defer(ephemeral=True, thinking=True)
                res = await app.control.kill(Actor.DISCORD, "slash /kill", who=str(interaction.user.id))
                await interaction.followup.send(
                    f"🛑 KILLED. {res['cancelled']} orders cancelled.", ephemeral=True
                )

        @tree.command(name="flatten", description="Kill and exit every position (asks for confirmation)")
        async def flatten(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                await interaction.response.send_message(
                    "Confirm kill + flatten:", view=ConfirmFlatten(self), ephemeral=True
                )

        @tree.command(name="autotrade", description="Turn automatic trading on/off")
        async def autotrade(interaction: discord.Interaction, on: bool) -> None:
            if await self.authorized(interaction):
                await app.control.set_autotrade(on, Actor.DISCORD, who=str(interaction.user.id))
                await interaction.response.send_message(
                    f"🤖 Autotrade {'ON' if on else 'OFF'}.", ephemeral=True
                )

        @tree.command(name="limits", description="Show risk limits")
        async def limits(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                await interaction.response.send_message(
                    embed=fmt.limits_embed(app.risk.limits.model_dump()), ephemeral=True
                )

        @tree.command(name="setlimit", description="Change a risk limit, e.g. max_markets_per_day 5")
        async def setlimit(interaction: discord.Interaction, name: str, value: float) -> None:
            if await self.authorized(interaction):
                try:
                    cur = app.risk.limits.model_dump().get(name)
                    v: Any = int(value) if isinstance(cur, int) and not isinstance(cur, bool) else value
                    res = await app.control.set_limits({name: v}, Actor.DISCORD, who=str(interaction.user.id))
                    await interaction.response.send_message(
                        f"✅ {res['changed'] or 'no change'}", ephemeral=True
                    )
                except (ValueError, Exception) as exc:
                    await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

        @setlimit.autocomplete("name")
        async def _limit_names(_: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            names = [n for n in app.risk.limits.model_dump() if current.lower() in n]
            return [app_commands.Choice(name=n, value=n) for n in names[:25]]

        @tree.command(name="panel", description="Post the control panel")
        async def panel(interaction: discord.Interaction) -> None:
            if await self.authorized(interaction):
                await interaction.response.send_message(embed=self.status_embed(), view=ControlPanel(self))

    # ------------------------------------------------------------------ notifications

    async def _notifier(self) -> None:
        await self.wait_until_ready()
        n = self.cfg.notify
        sub = self.app.bus.stream(
            FillReceived,
            EngineStateChanged,
            Alert,
            DailyReport,
            MarketSettled,
            RiskLimitsChanged,
            maxsize=5000,
        )
        interval = self.cfg.min_message_interval_s
        while not self.is_closed():
            evt: Event = await sub.get()
            batch = [evt]
            await asyncio.sleep(interval)  # coalesce bursts (fills especially)
            while (more := sub.get_nowait()) is not None:
                batch.append(more)
            ch = await self.channel()
            if ch is None:
                continue
            embeds: list[discord.Embed] = []
            fills = [ser.fill_dict(e.fill) for e in batch if isinstance(e, FillReceived)]
            if fills and n.fills:
                embeds.append(fmt.fills_embed(fills))
            for e in batch:
                if isinstance(e, EngineStateChanged) and n.engine_state:
                    embeds.append(
                        fmt.alert_embed(
                            "warning" if e.new.value != "running" else "info",
                            f"Engine {e.old.value} → {e.new.value.upper()}",
                            f"by {e.actor.value}: {e.reason}",
                        )
                    )
                elif isinstance(e, Alert) and n.alerts and (e.level != "info" or n.risk):
                    embeds.append(fmt.alert_embed(e.level, e.title, e.message))
                elif isinstance(e, DailyReport) and n.daily_report:
                    embeds.append(fmt.daily_report_embed(e.report))
                elif isinstance(e, MarketSettled):
                    pnl = e.pnl_micros / MICROS_PER_DOLLAR
                    embeds.append(
                        fmt.alert_embed(
                            "info", f"Settled {e.ticker} → {e.result.upper()}", f"P&L {fmt.usd(pnl)}"
                        )
                    )
                elif isinstance(e, RiskLimitsChanged) and n.risk:
                    embeds.append(
                        fmt.alert_embed("info", "Risk limits changed", f"{e.changes} by {e.actor.value}")
                    )
            for i in range(0, len(embeds), 10):  # Discord: ≤10 embeds per message
                try:
                    await ch.send(embeds=embeds[i : i + 10])
                except Exception as exc:
                    log.warning("discord_send_failed", error=str(exc))


async def run_bot(app: Application) -> None:
    cfg = app.settings.discord
    if not cfg.token:
        log.warning("discord_disabled_no_token")
        return
    bot = KstBot(app)
    app.discord_bot = bot
    try:
        await bot.start(cfg.token.get_secret_value())
    finally:
        with contextlib.suppress(Exception):
            await bot.close()
