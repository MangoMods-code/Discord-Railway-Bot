def metrics_embed(
    project_name: str,
    service_name: str,
    env_name: str,
    cpu_pct: float,
    mem_bytes: int,
    mem_limit_bytes: int,
) -> discord.Embed:
    mem_pct = (mem_bytes / mem_limit_bytes * 100) if mem_limit_bytes else 0
    is_breach = cpu_pct >= cfg.CPU_THRESHOLD_PCT or mem_pct >= cfg.MEMORY_THRESHOLD_PCT
    color = discord.Color.red() if is_breach else EMBED_COLOR
    title_prefix = "🔥  Threshold Breached" if is_breach else "📊  Resource Usage"
    embed = discord.Embed(title=f"{title_prefix} — {project_name}", color=color)
    embed.add_field(name="Service", value=service_name, inline=True)
    embed.add_field(name="Environment", value=env_name, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    cpu_flag = " ⚠️" if cpu_pct >= cfg.CPU_THRESHOLD_PCT else ""
    mem_flag = " ⚠️" if mem_pct >= cfg.MEMORY_THRESHOLD_PCT else ""
    embed.add_field(name="CPU", value=f"**{cpu_pct:.1f}%**{cpu_flag}", inline=True)
    embed.add_field(
        name="Memory",
        value=f"**{format_bytes(mem_bytes)}** / {format_bytes(mem_limit_bytes)} ({mem_pct:.1f}%){mem_flag}",
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    return railway_footer(embed)
