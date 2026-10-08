"""Samsung, March 2023, replayed.

Three engineers pasted source code and the contents of a meeting into a public chatbot. Samsung's
first control was a cap of 1,024 bytes per prompt. This module runs prompts of the same kinds,
all invented, through that cap and through the prompt guard, side by side.

The first three scenarios are the three incidents. The last two show that a size cap fails in
both directions: a short prompt can hold a secret, and a long one can be harmless. ``sensitive``
is the truth by construction, so each control's decisions can be counted as right or wrong.

Every name, number, host and credential here is made up.
"""
from __future__ import annotations

import copy

from .config import GuardPolicy, Settings
from .guard import Guard

SAMSUNG_CAP = 1024  # bytes per prompt (April 2023)
TERMS = ["Project Halcyon", "Halcyon"]  # the invented organisation's confidential terms
_DB_PASSWORD = "Wafer!" + "Lot42"
_LOGIN_PASSWORD = "Tr1ton!" + "Vault#88"

INCIDENT_1 = '''This loader crashes on some wafers with "KeyError: site". Can you find the bug?

# metrology/loader.py  (maintainer: Dana Whitlock <dana.whitlock@northfab.example>)
import psycopg2

MEAS_DB = "postgres://meas_ro:DB_PASSWORD@measdb.fab3.northfab.example:5432/metrology"


def load_wafer(lot_id, wafer_no):
    conn = psycopg2.connect(MEAS_DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT site, cd_nm, overlay_nm, tool_id FROM cd_measurements "
        "WHERE lot_id = %s AND wafer_no = %s ORDER BY site",
        (lot_id, wafer_no),
    )
    sites = {}
    for site, cd_nm, overlay_nm, tool_id in cur.fetchall():
        sites[site] = {"cd": cd_nm, "overlay": overlay_nm, "tool": tool_id}
    return sites


def out_of_spec(lot_id, wafer_no, target_nm=18.0, tol_nm=1.2):
    sites = load_wafer(lot_id, wafer_no)
    bad = []
    for n in range(1, 50):
        m = sites[n]
        if abs(m["cd"] - target_nm) > tol_nm or abs(m["overlay"]) > 2.5:
            bad.append((n, m["tool"]))
    return bad


def tool_report(lot_id, wafers):
    """Sites out of spec per tool, for the morning yield meeting."""
    per_tool = {}
    for wafer_no in wafers:
        for site, tool in out_of_spec(lot_id, wafer_no):
            per_tool.setdefault(tool, []).append((wafer_no, site))
    lines = [f"Lot {lot_id}: {len(per_tool)} tool(s) with sites out of spec"]
    for tool in sorted(per_tool, key=lambda t: -len(per_tool[t])):
        lines.append(f"  {tool}: {len(per_tool[tool])} site(s)")
    return "; ".join(lines)
'''.replace("DB_PASSWORD", _DB_PASSWORD)

INCIDENT_2 = '''Make this faster, it runs on every lot.

def flag_drifting_tools(lots, yield_floor=0.88, min_lots=5):
    by_tool = {}
    for lot in lots:
        for tool in lot.route:
            by_tool.setdefault(tool, []).append(lot.die_good / lot.die_total)
    drifting = []
    for tool, yields in by_tool.items():
        if len(yields) >= min_lots and sum(yields[-min_lots:]) / min_lots < yield_floor:
            drifting.append(tool)
    return sorted(drifting)
'''

INCIDENT_3 = '''Can you turn these notes into formal minutes?

Fab 3 weekly yield review, 14 March
Present: Dana Whitlock, Marcus Oyelaran, Ines Carvalho
- Line 4 yield fell from 91.2% to 86.7% after the Halcyon recipe change
- Marcus thinks etch tool ET-07 is drifting; Ines will pull the measurement logs
- The customer shipment for Project Halcyon slips two weeks if yield stays under 88%
- Dana to brief the VP on Friday; call her on (408) 555-0177 if the numbers change
- Send the corrected lot list to ines.carvalho@northfab.example before the next review
'''

SHORT_SECRET = ('The staging portal logs me out after one request. The admin login is dana.whitlock@northfab.example and the '
                'password is LOGIN_PASSWORD. Why would the session expire?').replace("LOGIN_PASSWORD", _LOGIN_PASSWORD)

LONG_HARMLESS = '''Please proofread this paragraph for a school science newsletter and suggest a clearer structure.

Making a computer chip starts with a thin, polished disc of silicon called a wafer. The wafer is coated with a material that reacts to light. A machine then shines light through a patterned plate, a little like a slide projector, so that the pattern lands on the coating. Where the light falls, the coating changes and can be washed away, which leaves the pattern behind on the wafer. After that, the exposed areas are etched or filled with other materials, and the remaining coating is removed. The whole cycle is repeated dozens of times, one layer on top of another, until millions of tiny switches and the wires that join them have been built up.

The hard part is size. The features on a modern chip are far smaller than a speck of dust, so the work is done in rooms where the air is filtered again and again. A single particle in the wrong place can ruin a chip. This is also why the share of chips on a wafer that work, which engineers call the yield, matters so much: Every chip that fails is cost with nothing to show for it. When a factory is new the yield is often low, and much of an engineer's work is finding out which step is going wrong and fixing it.
'''

SCENARIOS: list[dict] = [
    {"id": "incident-1", "title": "Incident 1: debug the measurement database loader", "sensitive": True,
     "samsung": "An engineer pasted the source code of a program tied to the equipment measurement database, to fix a bug.",
     "holds": "Internal source code, a database password, an internal host name, a maintainer's name and e-mail",
     "prompt": INCIDENT_1},
    {"id": "incident-2", "title": "Incident 2: optimise the yield and faulty-equipment code", "sensitive": True,
     "samsung": "A second engineer pasted code for yield and for identifying defective equipment, and asked for it to be optimised.",
     "holds": "Internal source code that shows how yield is measured and tools are flagged", "prompt": INCIDENT_2},
    {"id": "incident-3", "title": "Incident 3: turn meeting notes into minutes", "sensitive": True,
     "samsung": "A third employee gave the contents of an internal meeting and asked for the minutes.",
     "holds": "Three names, a phone number, an e-mail address, a project codename, yield figures",
     "prompt": INCIDENT_3},
    {"id": "short-secret", "title": "A short question with a password in it", "sensitive": True,
     "samsung": "Not one of the three incidents: the kind of prompt the 1,024-byte cap was blind to.",
     "holds": "A login and its password, in two sentences", "prompt": SHORT_SECRET},
    {"id": "long-harmless", "title": "A long prompt with nothing sensitive in it", "sensitive": False,
     "samsung": "Not one of the three incidents: the kind of prompt the 1,024-byte cap refused for no reason.",
     "holds": "Nothing: general knowledge for a school newsletter", "prompt": LONG_HARMLESS},
]


def _guard_outcome(r: dict) -> str:
    if r["verdict"] == "blocked":
        return "Refused: " + "; ".join(b["detail"] for b in r["blocks"])
    if r["verdict"] == "redacted":
        return f"Sent with {r['values']} value(s) replaced by tokens"
    return "Sent unchanged: nothing sensitive was found"


def run(cap: int = SAMSUNG_CAP, settings: Settings | None = None, policy: GuardPolicy | None = None) -> dict:
    """Each scenario under a size cap of ``cap`` bytes and under the prompt guard (the
    organisation's policy unless ``policy`` is given). Each scenario is its own conversation."""
    s = copy.copy(settings or Settings())
    s.confidential_terms = list(s.confidential_terms) + TERMS
    policy = policy or GuardPolicy.default()
    rows = []
    for sc in SCENARIOS:
        r = Guard().check(sc["prompt"], s, policy)
        capped = r["bytes"] > cap
        # A sensitive prompt is handled rightly if it is stopped or stripped; a harmless one, if it goes through whole.
        cap_right = capped == sc["sensitive"]
        guard_right = (r["verdict"] in ("blocked", "redacted")) if sc["sensitive"] else r["verdict"] == "clean"
        rows.append({
            **{k: sc[k] for k in ("id", "title", "samsung", "holds", "sensitive", "prompt")},
            "bytes": r["bytes"],
            "cap": {"verdict": "blocked" if capped else "allowed", "right": cap_right,
                    "outcome": f"Refused unread: {r['bytes']:,} bytes is over the cap" if capped
                    else f"Sent as written: {r['bytes']:,} bytes fits under the cap"},
            "guard": {"verdict": r["verdict"], "right": guard_right, "outcome": _guard_outcome(r), "blocks": r["blocks"],
                      "warnings": r["warnings"], "values": r["values"], "by_category": r["by_category"],
                      "code_lines": r["code_lines"], "languages": r["code"]["languages"], "safe_text": r["safe_text"]},
        })
    sensitive = [x for x in rows if x["sensitive"]]
    return {
        "cap_bytes": cap, "policy": {"source_code": policy.source_code, "markings": policy.markings},
        "terms": TERMS, "scenarios": rows,
        "summary": {
            "scenarios": len(rows),
            "cap_right": sum(x["cap"]["right"] for x in rows), "guard_right": sum(x["guard"]["right"] for x in rows),
            "cap_leaks": sum(x["cap"]["verdict"] == "allowed" for x in sensitive),
            "guard_leaks": sum(x["guard"]["verdict"] == "clean" for x in sensitive),
            "cap_refused_harmless": sum(x["cap"]["verdict"] == "blocked" for x in rows if not x["sensitive"]),
            "guard_refused_harmless": sum(x["guard"]["verdict"] != "clean" for x in rows if not x["sensitive"]),
        },
    }
