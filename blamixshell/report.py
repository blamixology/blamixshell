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


# ---------------------------------------------------------------- the same report as data (report --json)
def _plain(x):
    """Dataclasses, tuples and lists -> JSON-ready dicts and lists."""
    import dataclasses
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        out = {f.name: _plain(getattr(x, f.name)) for f in dataclasses.fields(x)}
        for name in dir(type(x)):                       # computed values too (a disk's percent, memory percent)
            if not name.startswith("_") and isinstance(getattr(type(x), name), property):
                try:
                    out[name] = _plain(getattr(x, name))
                except Exception:
                    pass
        return out
    if isinstance(x, (list, tuple, set)):
        return [_plain(v) for v in x]
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return str(x)


def as_data(label: str, address: str, when: datetime, data: dict, errors: dict | None = None) -> dict:
    """The report as a dict for scripts: every part as collected (None when it couldn't be read, with the reason in
    "errors"), plus a short "summary" of what usually matters."""
    errors = errors or {}
    out: dict = {"server": label, "address": address, "created": when.isoformat(timespec="seconds"),
                 "errors": dict(errors)}
    ov = data.get("overview")
    out["overview"] = _plain(ov) if ov is not None else None
    sy = data.get("system")
    out["system"] = _plain(sy) if sy is not None else None
    sv = data.get("services")
    out["services"] = None if sv is None else {"note": sv[1], "list": _plain(sv[0]),
                                               "failed": [s.unit for s in sv[0] if s.failed]}
    up = data.get("updates")
    out["updates"] = None if up is None else {"manager": up[0], "packages": _plain(up[1])}
    out["ports"] = _plain(data["ports"]) if data.get("ports") is not None else None
    us = data.get("users")
    out["users"] = None if us is None else {"accounts": _plain(us[0]), "sessions": _plain(us[1]),
                                            "groups": _plain(us[2]) if len(us) > 2 else []}
    out["cron"] = _plain(data["cron"]) if data.get("cron") is not None else None
    f = data.get("firewall")
    out["firewall"] = _plain(f) if f is not None else None

    disks = [x.percent for x in (ov.disks if ov is not None else [])]
    out["summary"] = {
        "failed_services": len(out["services"]["failed"]) if sv is not None else None,
        "pending_updates": len(up[1]) if up is not None else None,
        "reboot_required": getattr(sy, "reboot_required", None) if sy is not None else None,
        "fullest_disk_percent": round(max(disks)) if disks else None,
        "memory_percent": round(ov.mem_percent) if ov is not None and ov.mem_total_kb else None,
        "listening_ports": len(data["ports"]) if data.get("ports") is not None else None,
        "firewall": (f"{f.manager} {f.state}".strip() if f is not None else None),
    }
    return out
