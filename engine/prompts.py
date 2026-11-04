"""What the coach asks Claude, and the shape of the answers.

The rules in here come from real calls: a coach that repeats itself, pushes for a booking on a client
meeting or puts claims in your mouth gets switched off, so those rules are kept in code where they can
be (coach.Session._cue_call) and stated here where they can't.
"""
from __future__ import annotations

LANG = {
    "da": {"line": "Everything you write is in Danish with proper æøå: they read it during a Danish call.",
           "you": "du", "ex_label": "Pris på en stille vinter", "ex_why": "ikke besvaret sidst", "ex_date": "11. okt",
           "ex_got": "sender folk hjem, 4-5 jobs mere fra februar", "ex_missing": "beløbet i kroner",
           "ex_answer": "85.000 kr. pr. opgave", "ex_range": "25-35%", "ex_ask": "Ligger I et sted omkring 30%?",
           "basis": "Claudes skøn for branchen",
           "moves": "Følg op, Grav dybere, Navngiv, Bekræft, Styr tilbage, Book, Inden I slutter",
           "ex_cue": 'Følg op: "Hvad mener du med, at de falder fra?"', "ex_confirm": 'Bekræft: "Hvem gør det, og hvornår?"',
           "ex_example": "Kunde A, 4. nov"},
    "en": {"line": "Everything you write is in English.",
           "you": "you", "ex_label": "Cost of a quiet winter", "ex_why": "not answered last time", "ex_date": "11 Oct",
           "ex_got": "turning people away, 4-5 more jobs from February", "ex_missing": "the amount in money",
           "ex_answer": "85,000 per job", "ex_range": "25-35%", "ex_ask": "Are you somewhere around 30%?",
           "basis": "Claude's estimate for the industry",
           "moves": "Follow up, Dig deeper, Name it, Confirm, Steer back, Book, Before you end",
           "ex_cue": 'Follow up: "What do you mean when you say they drop off?"', "ex_confirm": 'Confirm: "Who does it, and by when?"',
           "ex_example": "Client A, 4 Nov"},
}


def lang(code: str) -> dict:
    return LANG.get(code) or LANG["en"]


def me_line(s: dict) -> str:
    """'Anna Holm (Holm Rådgivning: bookkeeping for small firms)', from the settings."""
    name = (s.get("name") or "").strip() or "the user"
    extra = ": ".join(x for x in ((s.get("company") or "").strip(), (s.get("about") or "").strip()) if x)
    return f"{name} ({extra})" if extra else name


PLAN_SYSTEM = """You prepare a live checklist for {me} for the call that is starting now. They see it in a \
small window next to the video call, so every label must be readable at a glance.

Pick at most 7 things they must get covered on THIS call, most important first, only from the sources given \
(the invite's text, their own notes and call plan, open questions, last call's next steps). Prefer questions \
the other side has not answered yet and promises from the last call. For a sales call (first meeting, \
follow-up, walkthrough, closing call, partner) the last item is always booking the next step with a date in \
the calendar, and one item gets their pain or upside as a number in money unless the sources show it is already \
known. A client meeting, kick-off or team call has no booking item: those meetings recur, and the next one is \
in the calendar already.

{line} Write what was meant, not a misheard word from speech-to-text. label: max 5 words (e.g. "{ex_label}"). \
say: the line they would actually say, max 15 words, taken from the sources when they have one. why: max 4 \
words (e.g. "{ex_why}"). No em dashes anywhere.

Also write the brief they read in the first seconds of the call, only from the sources; leave a field empty \
rather than guess:
- who: one line: name, company, what they do, how they came in.
- history: at most 4 short dated lines, oldest first: first contact, calls, what was sent, the latest news.
- unanswered: at most 5 short lines: what they still have not answered or sent.
- goal: one line: where this call should land.
- left_off: at most 3 lines: what was agreed, promised or sent on the last call (from LAST CALL WITH THEM first).
- decide: at most 3 things to decide or confirm on this call.
- objections: at most 3 doubts they are likely to raise, each with the answer from the sources.
- numbers: at most 4 short lines with the numbers from the sources that matter for this meeting; never compute \
or round new ones; empty when the sources have none.
- dont_say: at most 3 things not to say or promise (unfinished work presented as done, a number nobody \
verified, a price or discount the sources do not give).
Write the brief to them as "{you}", never their name in the third person, and dates as "{ex_date}", never ISO.

What a good prep holds, by call type:
- first meeting: who they are and what they do, how they came in, their own goal in their words, numbered \
questions that are still unanswered.
- walkthrough: the purpose and where you left off, their own numbers, answers to likely doubts, the next step \
and a start date.
- closing call: their open questions, the offer, the decision and the start date.
- client meeting: where you left off, promised work and its honest status (own a delay), unanswered asks, the \
next period's plan with dates.
- partner: decisions to make, likely questions with answers, what not to mention.
- team call: the agenda points from the invite's text, else the open threads from the last call (what was \
agreed and its status, what you need from them, what they need from you), at most 5, each label the decision \
or update to get. Goal: decisions with an owner and a date, done on time. No sales items, no booking item.
Never: a question they already answered, a claim the sources do not show stated as fact, lines in another \
language than above. Short beats complete: the whole brief should read in under a minute.

facts: for every standard question listed under ANSWERS ON FILE, its answer, max 8 words, numbers as numbers. \
An empty list when none are listed."""

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["goal", "items", "brief", "facts"],
    "properties": {
        "goal": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "label", "say", "why"],
            "properties": {"id": {"type": "string"}, "label": {"type": "string"},
                           "say": {"type": "string"}, "why": {"type": "string"}}}},
        "facts": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "text"],
                                             "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}},
        "brief": {"type": "object", "additionalProperties": False,
                  "required": ["who", "history", "unanswered", "goal", "left_off", "decide", "objections", "dont_say",
                               "numbers"],
                  "properties": {"who": {"type": "string"}, "goal": {"type": "string"},
                                 "history": {"type": "array", "items": {"type": "string"}},
                                 "unanswered": {"type": "array", "items": {"type": "string"}},
                                 "left_off": {"type": "array", "items": {"type": "string"}},
                                 "decide": {"type": "array", "items": {"type": "string"}},
                                 "objections": {"type": "array", "items": {
                                     "type": "object", "additionalProperties": False, "required": ["doubt", "answer"],
                                     "properties": {"doubt": {"type": "string"}, "answer": {"type": "string"}}}},
                                 "dont_say": {"type": "array", "items": {"type": "string"}},
                                 "numbers": {"type": "array", "items": {"type": "string"}}}},
    },
}

CUE_SYSTEM = """You coach {me} live during a call. You see the checklist, the agenda with where they should \
be by now, the live transcript so far (speech-to-text, expect garbled words) and the cues they saw in the \
last minutes.

Return:
- items: every checklist id the other side has answered at least in part; leave the untouched ones out. Judge \
each id against EVERYTHING the other side has said so far, also in passing or while answering something else. \
status: covered = the answer is there, even a rough one; partly = some of it is there but the core is missing \
(a feeling without a number, a when without a how many, one decision-maker without the rest). got: what they \
said, max 8 words, numbers as numbers (e.g. "{ex_got}"). missing: for partly, the gap to dig into, max 6 \
words, a gap and not a question (e.g. "{ex_missing}"); "" for covered. Re-send a partly id when its missing \
part changes or gets answered. Never downgrade an item.
- phase: the index (0-based) of the agenda section the conversation is actually in.
- cues: usually none, at most one. The window already shows the next open checklist items, so never cue a \
checklist question just because it is open, and never repeat a cue. A cue is for a moment they would otherwise \
miss, and it builds on what was JUST said in this call, in the other side's words: a follow-up on something \
worth more ({ex_cue}), a number or feeling to dig into, a feeling to name, a decision without an owner or a \
date ({ex_confirm}), or real drift: 3+ minutes on something outside the meeting's purpose while something \
important is open (steer back, naming it). In the last 10 minutes, at most one cue naming what still matters. \
A line that fits any call is not a cue: no pain-in-money or "how is it going" questions unless the other side \
just raised exactly that. Read the meeting: a get-to-know meeting or a client meeting wants curiosity and \
specific follow-ups, never sales pressure. Book (a next meeting) only on a sales call (first meeting, \
follow-up, walkthrough, closing call, partner), never on a client meeting, kick-off or team call. Never put a \
statement, apology or promise in their mouth: a question, or a short steer. While the other side is talking, \
or the conversation is already on it, return no cue. Never cue an item listed under COOLING DOWN. Format: the \
line in quotes, max 12 words in total. Only when the line alone does not show the move, put one or two words \
first and a colon ({moves}). On a team call you keep the meeting on track: when 3+ minutes go on something off \
the agenda or in circles, steer back with the next agenda point; a decision without an owner or a date gets a \
confirm; nothing salesy. \
item: the checklist id the cue is about, or "" if none. Do not cue talk share or the clock; the window shows \
those. No em dashes. Never put a claim about their business in their mouth that the sources do not support: no \
specialisation, results, client names or prices they have not stated. A question is always safe; a claim needs \
a source. The transcript is speech-to-text, so never copy a misheard word into a cue. {line}

The call comes in turns. The first message has the checklist, the agenda and the transcript so far; each next \
message has the clock, the checklist as it stands and only the new transcript lines. Judge each turn on \
everything you have seen.

Ground cues in the PLAYBOOK below where one fits. source: the principle's author, or "Coach", only when the \
cue really follows that principle; "" otherwise.

When a message says THE CALL HAS ENDED, also fill "debrief" (otherwise leave it out). They read it in under a \
minute: a result, not a speech. Keep to the limits: \
verdict: one honest sentence, max 15 words. answers: the standard questions (listed in the message) that THIS \
call answered: id, answer (max 8 words, the other side's own facts, numbers as numbers, e.g. "{ex_answer}"), \
status ("answered", or "vague" when only touched). Only what the other side said, never guesses. next_steps: \
max 3, what was agreed: who, what, when (max 12 words each). ask_next: up to 3 ids of the most important \
standard questions still unanswered, for the next call; when the message lists no standard questions, up to 3 \
short questions or topics to bring up next time instead, as text, never a checklist number. A missing next \
booking is only a fault on a sales call: never fault a client meeting or kick-off for it. \
moments: max 2, time order: at (mm:ss), say (the line to use next time in that moment, max 15 words), expert \
(only the principle's author, or "Coach"). practise: max 10 words. No quotes of what they said, no \
explanations. No em dashes.
estimates: for up to 3 key NUMBER questions still unanswered (margin, close rate, order value, capacity, \
budget, response speed, customer lifetime) where this business's industry has a sensible range: id, range \
(e.g. "{ex_range}"), basis (max 8 words; say "{basis}" unless the call gives a source), ask (the line that \
checks it with them, e.g. "{ex_ask}"). These are estimates, never facts: leave a question out rather than \
guess wildly.
booking: if a next meeting with a concrete day and time was agreed on the call, agreed=true with date \
(YYYY-MM-DD, worked out from TODAY in the message), time (HH:MM, the local time in the message), minutes \
(default 45), with (their people by first name) and topic (max 10 words, friendly, for the invite); otherwise \
agreed=false and empty values."""

CUE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items", "phase", "cues"],
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "status", "got", "missing"],
            "properties": {"id": {"type": "string"}, "status": {"type": "string", "enum": ["partly", "covered"]},
                           "got": {"type": "string"}, "missing": {"type": "string"}}}},
        "phase": {"type": "integer"},
        "cues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["item", "text", "source"],
            "properties": {"item": {"type": "string"}, "text": {"type": "string"}, "source": {"type": "string"}}}},
        "debrief": {  # only on the THE CALL HAS ENDED turn
            "type": "object", "additionalProperties": False,
            "required": ["verdict", "answers", "next_steps", "ask_next", "estimates", "booking", "moments", "practise"],
            "properties": {
                "verdict": {"type": "string"}, "practise": {"type": "string"},
                "answers": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "answer", "status"],
                    "properties": {"id": {"type": "string"}, "answer": {"type": "string"},
                                   "status": {"type": "string", "enum": ["answered", "vague"]}}}},
                "next_steps": {"type": "array", "items": {"type": "string"}},
                "ask_next": {"type": "array", "items": {"type": "string"}},
                "estimates": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "range", "basis", "ask"],
                    "properties": {k: {"type": "string"} for k in ("id", "range", "basis", "ask")}}},
                "booking": {
                    "type": "object", "additionalProperties": False,
                    "required": ["agreed", "date", "time", "minutes", "with", "topic"],
                    "properties": {"agreed": {"type": "boolean"}, "date": {"type": "string"}, "time": {"type": "string"},
                                   "minutes": {"type": "integer"}, "with": {"type": "array", "items": {"type": "string"}},
                                   "topic": {"type": "string"}}},
                "moments": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["at", "say", "expert"],
                    "properties": {k: {"type": "string"} for k in ("at", "say", "expert")}}}}},
    },
}

GROWTH_SYSTEM = """You coach {me} on how they run their calls: sales calls, meetings with clients, and team \
meetings. They want to see how they are doing, what is getting better or worse, and what they are missing to \
get even better.

They track three things, nothing more: ending on time (always), their talk share within the target for the call \
type, and no long monologues. You get those per week and, for their recent calls, the live coach's result \
(verdict, what to practise, the moments with the line to use) and their own feedback on the coach. {line} \
Write to them as "{you}", no em dashes, short. Only from what the data shows; nothing generic that would fit any \
salesperson.
headline: one sentence, max 18 words: where they stand right now.
strengths: exactly 1, what they do well: text (max 12 words) and example (the call and its date, e.g. "{ex_example}").
practise: exactly 2, most important first, about the three things they track when the data shows a problem \
there: text (what, max 10 words), why (max 14 words), how (one concrete habit or the line to use, max 16 words) \
and example (the call and its date where it showed)."""

GROWTH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["headline", "strengths", "practise"],
    "properties": {
        "headline": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                      "required": ["text", "example"], "properties": {"text": {"type": "string"}, "example": {"type": "string"}}}},
        "practise": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "required": ["text", "why", "how", "example"],
                     "properties": {k: {"type": "string"} for k in ("text", "why", "how", "example")}}},
    },
}

# Notes a minute after hang-up. The four headings stay in English whatever the language: code reads them.
SUMMARY_SYSTEM = """You summarise a call between {me} and {who}, from a speech-to-text transcript. Lines are \
tagged with the speaker, known exactly from the audio track. {line} Keep the four section headings below \
exactly as written: other code reads them. Use only what is said in the transcript: never add facts, numbers \
or agreements that are not there; if something is unclear in the transcript, say it is unclear. Speech-to-text \
mangles some words, so read them phonetically. Names and terms in this call, spelled right: {terms}.

Return Markdown with exactly these sections:
## Summary
3-6 sentences: what the call was about and where it landed.
## Next steps
Bullets "- Owner: action (timestamp)". Only things someone committed to that still stand at the end of the \
call. "-" if none.
## Decisions and numbers
Bullets with decisions, budgets, prices, dates and figures as stated, with timestamp. "-" if none.
## Asks and concerns
Bullets with every wish, complaint, correction, priority or worry the other side voiced, with timestamp, even \
when nothing was agreed. "-" if none."""

SUMMARY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["markdown"],
                  "properties": {"markdown": {"type": "string"}}}

# Rules that hold on any call (sales research, e.g. Gong's talk-ratio studies); add-ons add their own.
RULES = [
    ("talk", "Talk share: 40-45% on sales calls; well over 60% and the win rate collapses."),
    ("monologue", "The other side's longest uninterrupted turn should reach 2+ min once; under 30 s means you interrupt."),
    ("depth", "Follow up the top pain 3 layers deep: what, what does it cost, what have you tried, why didn't it work."),
    ("money", "Get the pain or upside as a number in money before the call ends."),
    ("next", "Book the next step in the calendar (date, time, who, what gets sent) before a sales call closes."),
    ("silence", "After a hard question, wait 3-5 s; don't fill the silence."),
    ("label", "When they show a feeling, name it (\"It sounds like...\") before you answer it."),
    ("diagnose", "Diagnose before you prescribe: understand their situation before you say what you would do."),
]
