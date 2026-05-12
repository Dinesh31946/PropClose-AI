from typing import Any, Dict, List


def build_context(
    units: List[Dict[str, Any]],
    chunks: List[Dict[str, Any]],
    *,
    prioritize_inventory: bool = False,
    matched_unit_fact_lock: bool = False,
) -> str:
    header = ""
    if matched_unit_fact_lock and units:
        u0 = units[0]
        header = (
            "AUTHORITATIVE — MATCHED UNIT (CRM): This lead is pinned to **UNIT-1** only—the exact "
            f"inventory row `{u0.get('unit_name') or u0.get('id')}` for this enquiry. "
            "**For PRICE and CARPET / BUILT AREA of this enquiry, cite ONLY UNIT-1** "
            "(the numbers in that inventory line). Do **not** mix brochure pricing with this unit.\n"
            "**Project-level facts** (amenities, location, connectivity, developer, possession story, "
            "clubhouse, parking, specs that apply to the whole project) → use **BROCHURE EVIDENCE** "
            "whenever it contains them. A Locked/Hold/Sold status on UNIT-1 does **not** remove brochure "
            "facts about the overall development.\n\n"
        )
    elif prioritize_inventory:
        header = (
            "AUTHORITATIVE: For price / cost / budget / rent questions use ONLY "
            "INVENTORY EVIDENCE below. Ignore conflicting numbers in BROCHURE EVIDENCE.\n\n"
        )

    unit_lines = []
    for index, item in enumerate(units, start=1):
        unit_lines.append(
            f"[UNIT-{index}] Unit {item.get('unit_name')} | Config {item.get('configuration')} | "
            f"Area {item.get('carpet_area')} | Price {item.get('price')} | Status {item.get('status')}"
        )

    chunk_lines = []
    for index, item in enumerate(chunks, start=1):
        content = (item.get("content") or "").replace("\n", " ").strip()
        chunk_lines.append(f"[CHUNK-{index}] {content[:500]}")

    return header + (
        "INVENTORY EVIDENCE:\n"
        + ("\n".join(unit_lines) or "NONE")
        + "\n\nBROCHURE EVIDENCE:\n"
        + ("\n".join(chunk_lines) or "NONE")
    )
