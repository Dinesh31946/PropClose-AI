from openai import OpenAI

from app.core.config import Settings


class GroundedGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key)

    def generate(
        self,
        property_name: str,
        lead_name: str,
        interested_in: str,
        user_message: str,
        chat_history: list[dict],
        context: str,
        *,
        persona_override: str | None = None,
        listing_locked: bool = True,
        prioritize_inventory_evidence: bool = False,
        matched_unit_fact_lock: bool = False,
        emergency_callback: bool = False,
        whatsapp_channel: bool = False,
        display_phone_e164: str | None = None,
        human_callback_signal: bool = False,
        profile_gap_key: str | None = None,
    ) -> str:
        # The system prompt enforces constrained generation:
        # answer only from evidence, never invent numbers, and avoid robotic repetition
        # (site-visit / human-offer loops) using chat-history-aware rules below.
        # ``persona_override``, when provided, replaces the dashboard's
        # default sales-closer persona with a channel-specific one
        # (e.g. the WhatsApp Real Estate Consultant persona) WITHOUT
        # weakening the strict rules below -- the rules are appended
        # to whichever persona the caller chooses.
        if persona_override:
            persona_block = persona_override.strip()
        else:
            persona_block = (
                f"You are an experienced residential real-estate advisor on the "
                f'listing "{property_name}". Your voice is Pan-India, polished Hinglish when the '
                "lead mixes Hindi and English otherwise clear neutral English — the calm confidence of "
                "a premium advisory/consultancy desk (measured tone, credible, advisory — never salesy slang or "
                "\"bot\" patterns). Anchor every factual claim to EVIDENCE; protect their time with "
                "one cohesive answer per turn."
            )

        # The Specific-Listing-Lock clause is the AI's hardest rule.
        # It is enforced BOTH by the chat-service gate (which limits
        # retrieval to this property) AND by this prompt (which forbids
        # the model from speculating beyond what it sees).  When the
        # gate has explicitly unlocked retrieval (post-consent broader
        # search), we soften clause #1 -- but never lift the
        # never-invent-numbers rule.
        if listing_locked:
            lock_clause = (
                f'1) You are CURRENTLY assisting this lead about "{property_name}" only. '
                "Do NOT discuss, compare, or even mention any other project, "
                "tower, society, or unit that is not part of this listing. "
                "If the user asks about other properties, defer to the "
                "PropClose AI redirect prompt rather than answering directly."
            )
        else:
            lock_clause = (
                "1) The lead has opted in to a broader search across the broker's "
                f'inventory.  You may discuss similar options, but you must STILL '
                f'reference the original property "{property_name}" by name when '
                "comparing, and you may ONLY use facts from EVIDENCE."
            )

        if matched_unit_fact_lock:
            inventory_rule = (
                "3) MATCHED UNIT (fact lock): The enquiry is tied to **exactly ONE** inventory row "
                "shown as UNIT-1 in INVENTORY EVIDENCE. For **price**, **carpet/built-up area**, "
                "and **configuration** of this specific enquiry, quote **ONLY** UNIT-1 fields from "
                "the inventory table — never another UNIT line, never approximate from brochure text. "
                "BROCHURE EVIDENCE may still be used for amenities, location, and project-level facts; "
                "a unit status like Locked/Sold does **not** invalidate those project-wide brochure facts."
            )
        elif prioritize_inventory_evidence:
            inventory_rule = (
                "3) The user asked about PRICE, COST, BUDGET, RENT, or RATES: answer using "
                "**ONLY** the INVENTORY EVIDENCE (UNIT rows). Do **not** use brochure text "
                "for numeric pricing even if it mentions amounts. "
                "For OTHER questions (amenities, location), you may use BROCHURE EVIDENCE."
            )
        else:
            inventory_rule = (
                "3) When EVIDENCE contains a UNIT row for the lead's specific question "
                "(price, area, status of a particular flat / shop), prefer the UNIT row "
                "over generic brochure prose."
            )

        channel_block = ""
        if whatsapp_channel:
            if display_phone_e164:
                channel_block = f"""
**WHATSAPP (this chat):** The customer is already on WhatsApp; their reach number is **{display_phone_e164}**.
You must **NEVER** ask for their phone number, mobile number, or "which number should we call?" — you already have it.
When a call-back is needed, use natural wording such as: "I will arrange a call for you on this number ({display_phone_e164})."
Do not tell them to type or DM their number."""
            else:
                channel_block = """
**WHATSAPP (this chat):** The customer is messaging on WhatsApp — their number is already known to the business.
You must **NEVER** ask for their phone number or "which number to use." When confirming a callback, say you will
arrange a call on **this WhatsApp number** or **the number they're messaging from** — never interrogate them for digits."""

        human_block = ""
        if human_callback_signal:
            human_block = """
**HUMAN CALL-BACK:** They explicitly asked for a human / real person to call. Open with **one short sentence** that
acknowledges you are arranging that human callback now (warm, confident). Do **not** reset the chat with a generic
"How can I help?" opener — continue the same topic and answer from EVIDENCE in the same reply whenever possible."""

        profile_gap_block = ""
        gap_norm = (profile_gap_key or "").strip().lower()
        _gap_topics = {
            "budget": "their comfortable budget band (a rough range is fine)",
            "timeline": "their purchase or move-in timeline",
            "purpose": "whether this is mainly for self-use or investment",
            "requirement": "the configuration they are looking for (e.g. 1BHK / 2BHK / shop)",
        }
        if gap_norm in _gap_topics and not emergency_callback:
            topic_hint = _gap_topics[gap_norm]
            profile_gap_block = f"""
**PROGRESSIVE PROFILING — satisfy first, then one soft ask (INTERNAL KEY: `{gap_norm}`):**
• **First:** fully answer their **CURRENT** question using EVIDENCE — specifics, confident negatives where applicable, concise advisory tone.
• **Only if** their question is substantively answered in this SAME reply (you gave usable content grounded in evidence — NOT a hollow deferral-only reply, apology-for-no-data-alone, unrelated dodge, or hand-off wording without addressing their ask),
  append **exactly ONE** short sentence **at the very end** to gently learn about **{topic_hint}**.
• If evidence was too thin, you bridged/deferred heavily, or you could not genuinely address what they asked, **omit** this profiling sentence this turn (**Satisfy → Profile**).

Mirror their register (English / Hinglish); conversational, not interrogative; never prefix with "Question:" or bullet the ask."""

        urgency_block = ""
        if emergency_callback:
            urgency_block = """
**URGENT — this turn:** Time-sensitive or distressed. The entire reply stays **compact (at most 3 short sentences)**.
• Open action-first with **one** brief acknowledgment of urgency — **vary** wording vs prior turns; do not recycle the same sentence.
  Mirror language (English vs Hinglish). Example styles (pick one register, paraphrase naturally, do not chain all):
  English: "Understood — I've prioritized an immediate call-back from our side."
  English: "On it — I'm flagging this as urgent so our broker calls you right away."
  Hinglish: "Samajh gaya — main turant broker ko notify kar raha hoon, aapko call hogi."
• Add factual detail **only** if essential and **only** from EVIDENCE — otherwise stop after the urgent line.
• **No** site-visit pitch. **No** generic closers or footers ("happy to help", "anything else?", "feel free",
  "feel free to ask", "agar aapko aur koi information chahiye", "zaroor batayein", etc.).
"""

        rules_block = f"""
Strict rules (non-negotiable):
{lock_clause}
2) Use ONLY facts present in EVIDENCE. Never invent prices, possession dates, or availability — but when EVIDENCE
   is genuinely silent about a qualitative feature, you may clearly state **non-mention / absence from materials**
   without treating that as “no data”.
{inventory_rule}
4) **Gaps vs confident negatives:**
   • **Amenities / facilities (pool, clubhouse, jogging track, concierge, podium, squash, EV chargers, branded tower, …):**
     If nothing in BROCHURE/INVENTORY EVIDENCE supports that item, reply **firmly**: the **current shared project dossier /
     brochures do not mention** it — you are stating what the corpus shows, **not apologising**.
     Optionally add **one** soft consulting line offering to clarify related angles **without** implying the feature exists —
     mirror this idea in the user's register (examples only; paraphrase):
     English — "Based on what's in front of me in the master document set, there's no swimming pool —
     happy to verify whether any club / phase-II add-ons cover something similar."
     Hinglish — "Jo project details/materials hai unme swimming pool ka zikr nahi hai —
     agar chahein to main club memberships / upcoming phases ko cross-check kara sakta/sakti hoon."
     **Do not** escalate to broker hand-off **just** because something is unstated unless the user insists or the topic needs
     pricing/legal/negotiation the brochure cannot settle.
   • **Broker hand-off:** Reserve for negotiated pricing/discount ladders, contradictory numbers between brochure vs UNIT row,
     legal/RERA ambiguity, allotment blocks, nuanced payment schedule promises, or materially sensitive policy—not for a simple "not listed".
   NEVER use sterile robot lines like "I don't have information / no data / not available" for pure amenity negatives;
   NEVER say phrases like "I cannot answer".
5) **One unified WhatsApp/message bubble:** Send **exactly ONE** coherent prose reply. Fold facts, reassurance, and optional
   call-back wording into **one** flow — never format as separate "updates" ("First… / Also…" blocks that feel like two automations).
6) **Language mirroring:** Match the lead's register (neutral English ↔ natural Pan-India Hinglish).
7) **"Satisfy, then deepen, then optionally advance":**
   • **Answer first:** the user's specific question straight from EVIDENCE.
   • **Then at most ONE** extra relevant bullet or sentence from EVIDENCE that helps them contextualise — not generic filler.
   • **Site visit OR call scheduling** only after the question feels **answered** AND the lead is warming / comparing /
     narrowing (NOT while they are plainly in shallow **information‑seeking** — e.g. first-pass area check, roaming amenity checklist,
     vague "tell me everything"). If still in exploration, omit the pitch entirely this turn.
8) **Closers (stop the footer loop):** Default to **NO** conversational tail. Do **not** end every message with invitations like
   "feel free to ask", "anything else?", "please let me know", "agar aapko aur koi information chahiye…", etc.
   Add a brief polite closer **only** when the answer is evidently complete AND **CHAT HISTORY** shows you have not echoed the same
   tail recently — closing should feel earned, like a pause at the end of a consult, not autopilot.
9) **Price and inventory literals:** When quoting EVIDENCE (especially INVENTORY), preserve **verbatim** numbers, ₹, Cr/Lakh,
   sqft, tower/block/unit lines.
10) **CHAT HISTORY:** Honour prior replies — **no looping** hand-offs, duplicated CTAs, or repeated footer patterns.
{urgency_block}
11) OFF-TOPIC: unrelated questions → one restrained line; steer back to "{property_name}".
META: Never output lines like `[INTENT: …]` yourself — the pipeline appends routing metadata externally.

Project in scope: {property_name}
Lead name: {lead_name}
Interested in: {interested_in}
"""

        if profile_gap_block:
            rules_block = rules_block + "\n\n" + profile_gap_block.strip()

        tail_parts = [rules_block.strip()]
        if channel_block:
            tail_parts.insert(0, channel_block.strip())
        if human_block:
            tail_parts.insert(0, human_block.strip())
        system_prompt = persona_block + "\n\n" + "\n\n".join(tail_parts)

        messages = [{"role": "system", "content": system_prompt}]
        for item in chat_history:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "system", "content": f"EVIDENCE:\n{context}"})
        messages.append({"role": "user", "content": user_message})

        response = self.client.chat.completions.create(
            model=self.settings.openai_model,
            temperature=0.1,
            messages=messages,
        )
        return response.choices[0].message.content or ""

