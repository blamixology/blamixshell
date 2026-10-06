"""A Markdown report of one server (for a ticket, a handover or an audit), built from what the
dashboard collectors return. Each part may be missing: it then says why. No Qt here."""
from __future__ import annotations

from datetime import datetime

from . import dashboard as d


def _cell(x) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ").strip() or "–"


def table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return ""
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _missing(errors: dict, key: str) -> str:
    return f"_Not available: {errors.get(key) or 'not collected'}_"


def build(label: str, address: str, when: datetime, data: dict, errors: dict | None = None) -> str:
    errors = errors or {}
    md = [f"# Server report: {label}", "", f"- Address: `{address}`", f"- Created: {when:%Y-%m-%d %H:%M}"
          " by BlamixShell", ""]

    ov = data.get("overview")
    md.append("## Overview")
    if ov is None:
        md.append(_missing(errors, "overview"))
    else:
        rows = [["Host", ov.host], ["System", ov.os], ["Kernel", f"{ov.kernel} ({ov.arch})"],
                ["Uptime", d.human_uptime(ov.uptime_s) if ov.uptime_s else "–"],
                ["CPUs", ov.cpus or "–"],
                ["CPU use", f"{ov.cpu_percent:.0f}%" if ov.cpu_percent is not None else "–"],
                ["Load (1 / 5 / 15 min)", " / ".join(f"{x:.2f}" for x in ov.load)],
                ["Memory", f"{d.human_kb(ov.mem_used_kb)} of {d.human_kb(ov.mem_total_kb)} ({ov.mem_percent:.0f}%)"
                 if ov.mem_total_kb else "–"],
                ["Swap", f"{ov.swap_percent:.0f}% of {d.human_kb(ov.swap_total_kb)}" if ov.swap_total_kb else "none"],
                ["Logged-in sessions", ov.sessions], ["Service manager", ov.init or "–"]]
        md.append(table(["", ""], rows))
        if ov.disks:
            md += ["", "### Disks",
                   table(["Mounted on", "Type", "Size", "Used", "Free", "Use"],
                         [[x.mount, x.fstype, d.human_kb(x.size_kb), d.human_kb(x.used_kb), d.human_kb(x.avail_kb),
                           f"{x.percent:.0f}%"] for x in ov.disks])]
    md.append("")

    sy = data.get("system")
    if sy is not None:
        md.append("## System")
        rows = [["Time zone", f"{sy.tz_name or '–'} ({sy.tz_abbr} {sy.tz_offset})" if sy.tz_abbr else sy.tz_name or "–"],
                ["Server time", sy.local_time or "–"],
                ["Clock sync (NTP)", "synchronized" if sy.ntp_synced else "not synchronized" if sy.ntp_synced is False
                 else "unknown"],
                ["Reboot required", "yes" if sy.reboot_required else "no" if sy.reboot_required is False else "unknown"],
                ["Swap", ", ".join(f"{d.human_kb(s.size_kb)} {s.kind} {s.name}" for s in sy.swaps) or "none"]]
        md += [table(["", ""], rows), ""]

    md.append("## Services")
    sv = data.get("services")
    if sv is None:
        md.append(_missing(errors, "services"))
    else:
        services, why = sv
        failed = [s for s in services if s.failed]
        md.append(f"{len(services)} services listed, **{len(failed)} failed**." + (f" ({why})" if why else ""))
        if failed:
            md += ["", table(["Service", "State", "Description"], [[s.unit, f"{s.active}/{s.sub}", s.description]
                                                                  for s in failed])]
    md.append("")

    md.append("## Pending updates")
    up = data.get("updates")
    if up is None:
        md.append(_missing(errors, "updates"))
    else:
        mgr, items = up
        if not mgr:
            md.append("No supported package manager found.")
        elif not items:
            md.append(f"Up to date ({mgr}), from the server's cached package lists.")
        else:
            md += [f"**{len(items)}** updates available ({mgr}, from the server's cached package lists).", "",
                   table(["Package", "New version"], [[u.package, u.version] for u in items[:40]])]
            if len(items) > 40:
                md.append(f"\n…and {len(items) - 40} more.")
    md.append("")

    md.append("## Listening ports")
    pt = data.get("ports")
    if pt is None:
        md.append(_missing(errors, "ports"))
    elif not pt:
        md.append("Nothing is listening.")
    else:
        md.append(table(["Protocol", "Address", "Port", "Process"],
                        [[p.proto.upper(), "all interfaces" if p.address in ("*", "0.0.0.0", "::") else p.address,
                          p.port, p.process or "–"] for p in pt]))
    md.append("")

    md.append("## Accounts")
    us = data.get("users")
    if us is None:
        md.append(_missing(errors, "users"))
    else:
        accounts = [a for a in us[0] if not a.system]
        md.append(table(["Account", "UID", "Groups", "Shell", "Locked", "Logged in"],
                        [[a.name, a.uid, ", ".join(a.groups), a.shell,
                          "yes" if a.locked else ("no" if a.locked is False else "?"), a.logged_in or "–"]
                         for a in accounts]) or "No login accounts found.")
    md.append("")

    md.append("## Scheduled jobs (connected user)")
    cr = data.get("cron")
    if cr is None:
        md.append(_missing(errors, "cron"))
    elif not cr:
        md.append("No jobs in the crontab.")
    else:
        md.append(table(["Schedule", "Command", "State"], [[e.schedule, e.command, "on" if e.enabled else "off"]
                                                           for e in cr]))
    md.append("")

    md.append("## Firewall")
    f = data.get("firewall")
    if f is None:
        md.append(_missing(errors, "firewall"))
    elif f.manager == "none":
        md.append("No firewall tool found.")
    else:
        md.append(f"**{f.manager}**, {f.state or 'state unknown'}"
                  + (f", default zone {f.default_zone}" if f.default_zone else "")
                  + ("  \n_Rules need root to read._" if f.needs_root else ""))
        if f.rules:
            md += ["", table(["Where", "Type", "Rule", "Detail"], [[r.scope, r.kind, r.value, r.detail]
                                                                   for r in f.rules[:60]])]
    md.append("")
    return "\n".join(md).rstrip() + "\n"
