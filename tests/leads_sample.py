"""Made-up touchpoints for the lead tests, the local preview and the screenshots.

Every company, person and address is fictional (example.com-style domains)."""
from datetime import datetime, timedelta, timezone

from app.leads import Contact, Touch

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)


def _p(name: str, domain: str, role: str) -> Contact:
    first = name.split()[0].lower()
    return Contact(f"{first}@{domain}", name, role)


def sample_touches(now: datetime = NOW) -> list[Touch]:
    d = lambda days, hours=0: now - timedelta(days=days, hours=hours)  # noqa: E731
    asha = _p("Asha Verma", "juniper.example", "recruiter")
    ravi = _p("Ravi Menon", "juniper.example", "interviewer")
    lena = _p("Lena Park", "orbitlabs.example", "recruiter")
    tom = _p("Tom Reyes", "orbitlabs.example", "hiring_manager")
    nina = Contact("nina@talentbridge.example", "Nina Shah", "recruiter")  # An agency recruiter: two clients.
    kai = _p("Kai Moreau", "vela.example", "interviewer")
    mia = _p("Mia Lund", "kite.example", "recruiter")
    sam = _p("Sam Ortiz", "tessel.example", "recruiter")
    jo = _p("Jo Ibarra", "nimbus.example", "recruiter")
    leo = _p("Leo Grant", "lumen.example", "recruiter")
    zed = _p("Zed Alami", "quartz.example", "interviewer")
    pia = _p("Pia Novak", "harbor.example", "recruiter")
    out = [
        # Juniper: hot, interviewing, panel booked.
        Touch("gmail:j1", "email", d(12), "Juniper Robotics", "juniper.example", [asha], "Backend role at Juniper",
              "Recruiter reached out about a senior backend role.", "in", 0.6, "none", "sourced", "", "https://mail.google.com/mail/#all/j1", role="Senior Backend Engineer"),
        Touch("gmail:j2", "email", d(9), "Juniper Robotics", "juniper.example", [asha], "Re: Backend role at Juniper",
              "He shared his availability for a screen.", "out", None, "none", "screening"),
        Touch("cal:j3", "calendar", d(6), "Juniper", "juniper.example", [asha, ravi], "Juniper · Technical round", "Interview on the calendar", stage_hint="interviewing"),
        Touch("perf:j4", "evaluation", d(6, -3), "Juniper", "", [], "Technical scored 7.4/10", "Outcome: advanced.", cue="positive",
              stage_hint="interviewing", score=7.4, detail_url="/valr#performance"),
        Touch("gmail:j5", "email", d(2), "Juniper Robotics", "juniper.example", [asha], "Next steps: system design panel",
              "Moving forward to a system design panel next week.", "in", 0.8, "positive", "interviewing", "System design panel Tuesday"),
        Touch("cal:j6", "calendar", now + timedelta(days=4), "Juniper", "juniper.example", [asha, ravi, _p("Dev Rao", "juniper.example", "interviewer")],
              "Juniper · System design panel", "Interview on the calendar", stage_hint="interviewing"),
        # Orbit: offer.
        Touch("gmail:o1", "email", d(20), "Orbit Labs", "orbitlabs.example", [lena], "Your application at Orbit Labs",
              "Application received for Staff Engineer.", "in", 0.2, "none", "applied", role="Staff Engineer"),
        Touch("cal:o2", "calendar", d(14), "Orbit Labs", "orbitlabs.example", [lena, tom], "Orbit Labs hiring manager round", "Interview on the calendar", stage_hint="interviewing"),
        Touch("perf:o3", "evaluation", d(14, -2), "Orbit Labs", "", [], "Hiring manager scored 8.1/10", "Outcome: advanced.", cue="positive", stage_hint="interviewing", score=8.1),
        Touch("gmail:o4", "email", d(3), "Orbit Labs", "orbitlabs.example", [lena], "Offer from Orbit Labs",
              "Verbal offer; written offer to follow this week.", "in", 0.9, "positive", "offer", "Review the written offer"),
        # Agency recruiter Nina pitches Vela and Kite: shared person links the two leads.
        Touch("gmail:v1", "email", d(8), "Vela", "talentbridge.example", [nina], "Opportunity: Platform lead at Vela",
              "Agency recruiter suggested him for a platform lead role at Vela.", "in", 0.5, "none", "sourced", role="Platform Lead"),
        Touch("cal:v2", "calendar", d(4), "Vela", "vela.example", [nina, kai], "Vela screening call", "Interview on the calendar", stage_hint="screening"),
        Touch("gmail:k1", "email", d(5), "Kite", "talentbridge.example", [nina], "Kite is hiring a voice AI engineer",
              "Same agency recruiter pitched a voice AI role at Kite.", "in", 0.4, "none", "sourced"),
        Touch("gmail:k2", "email", d(3), "Kite", "kite.example", [mia, nina], "Kite: take-home assignment",
              "Take-home assignment shared, due in five days.", "in", 0.3, "none", "interviewing", "Submit the take-home by Tuesday"),
        # Tessel: rejected.
        Touch("gmail:t1", "email", d(16), "Tessel", "tessel.example", [sam], "Interview invite: Tessel", "Invited to a coding round.", "in", 0.4, "none", "interviewing"),
        Touch("perf:t2", "evaluation", d(13), "Tessel", "", [], "Technical scored 5.2/10", "Outcome: rejected.", cue="negative", stage_hint="closed-lost", score=5.2),
        Touch("gmail:t3", "email", d(11), "Tessel", "tessel.example", [sam], "Update on your application",
              "Regret to inform: not moving forward.", "in", -0.4, "negative", "closed-lost"),
        # Nimbus: lukewarm screening.
        Touch("gmail:n1", "email", d(10), "Nimbus", "nimbus.example", [jo], "Quick chat about Nimbus?", "Recruiter asked for a quick intro call.", "in", 0.3, "none", "screening"),
        # Lumen: ghosted (25 days quiet).
        Touch("gmail:l1", "email", d(25), "Lumen", "lumen.example", [leo], "Lumen application", "Application acknowledged.", "in", 0.1, "none", "applied"),
        # Quartz: one recording only, attached to its interview.
        Touch("cal:q1", "calendar", d(1), "Quartz", "quartz.example", [zed], "Quartz R1 interview", "Interview on the calendar", stage_hint="interviewing"),
        Touch("fathom:q2", "recording", d(1, -1), "", "quartz.example", [zed], "Recording: Quartz R1 interview",
              "Discussed streaming pipelines and on-call.", url="https://fathom.video/share/example", detail_url="/valr#performance"),
        # Harbor: expired (40 days).
        Touch("gmail:h1", "email", d(40), "Harbor", "harbor.example", [pia], "Harbor role", "Recruiter outreach for a data role.", "in", 0.2, "none", "sourced"),
    ]
    # A few more cold, single-touch leads so the globe has some spread.
    for i, (name, days) in enumerate([("Aster", 6), ("Birch", 9), ("Cobalt", 13), ("Dune", 17), ("Ember", 3),
                                      ("Fjord", 19), ("Garnet", 22), ("Helix", 27)]):
        person = _p(f"Rae {name}son", f"{name.lower()}.example", "recruiter")
        out.append(Touch(f"gmail:x{i}", "email", d(days), name, f"{name.lower()}.example", [person], f"{name}: an opportunity",
                         "Recruiter outreach.", "in", 0.1 * (i % 4), "none", "sourced"))
    return out


def fixture_emails():
    """(owner, [(Email, expected item)]) from tests/fixtures/lead_emails.json."""
    import json
    from email.utils import parseaddr
    from pathlib import Path
    from app.lead_classify import Email
    data = json.loads((Path(__file__).parent / "fixtures" / "lead_emails.json").read_text())
    out = []
    for i, item in enumerate(data["emails"]):
        name, address = parseaddr(item["from"])
        to = [parseaddr(item.get("to", data["owner"]))]
        out.append((Email(id=item["id"], thread_id="t-" + item["id"], at=NOW - timedelta(hours=i),
                          labels=item.get("labels", ["INBOX"]), from_name=name, from_email=address.lower(),
                          to=[(n, a.lower()) for n, a in to], subject=item["subject"],
                          headers=item.get("headers", {}), text=item["text"], snippet=item["text"][:120]), item))
    return data["owner"], out


FIRST = ("Arc", "Bright", "Cinder", "Delta", "Echo", "Fable", "Glint", "Hollow", "Iris", "Jade", "Kestrel", "Lark",
         "Maple", "North", "Onyx", "Pike", "Quill", "Rook", "Sable", "Tide", "Umber", "Vireo", "Wren", "Yarrow")
SECOND = ("works", "loop", "forge", "stack", "path", "grid", "field", "base", "mint", "line")


def live_shaped_touches(now: datetime = NOW) -> list[Touch]:
    """About 115 fictional leads shaped like the owner's real mailbox on 2026-10-16 (70 live): many
    closed (rejections), a lump of fresh applications, a few warm and hot ones, some gone
    quiet and a few expired. Deterministic; every name and domain is made up."""
    d = lambda days: now - timedelta(days=days)  # noqa: E731
    names = iter(f"{a}{b}" for b in SECOND for a in FIRST)
    out: list[Touch] = []

    def lead(kind: str, i: int) -> None:
        name = next(names)
        dom = f"{name.lower()}.example"
        rec = Contact(f"talent@{dom}", f"Sam {name[:4]}", "recruiter")
        tid = f"t-{name}"
        mail = lambda key, days, **kw: Touch(f"gmail:{name}-{key}", "email", d(days), name, dom,  # noqa: E731
                                             kw.pop("people", [rec]), f"{name}: {key}", kw.pop("summary", ""),
                                             thread=tid, **kw)
        meet = lambda key, days, hint="interviewing": Touch(f"cal:{name}-{key}", "calendar", d(days), name, dom,  # noqa: E731
                                                            [rec], f"{name} · {key}", stage_hint=hint)
        ack = dict(people=[], direction="in", sentiment=0.1, stage_hint="applied", category="application_update",
                   action_requested=False)
        if kind == "hot":
            out.extend([mail("screen", 12 + i, direction="in", sentiment=0.6, stage_hint="screening"),
                        meet("Technical round", 6 + i), mail("next", 2 + i % 2, direction="in", sentiment=0.8, cue="positive",
                                                            stage_hint="interviewing", next_step="Panel next week"),
                        meet("Panel", -(1 + i))])
            if i == 0:
                out.append(mail("offer", 1, direction="in", sentiment=0.9, cue="positive", stage_hint="offer",
                                category="offer", action_requested=True, action="decide on the offer by Friday"))
        elif kind == "warm":
            asks = i % 3 == 0
            out.extend([mail("applied", 9 + i, **ack),
                        mail("reply", 1 + i % 5, direction="in", sentiment=0.5 + 0.05 * (i % 4), stage_hint="screening",
                             category="scheduling" if asks else "application_update", action_requested=asks,
                             action="share your availability for a screen" if asks else "")])
            if i % 4 == 1:
                out.append(meet("Intro call", 2 + i % 3, "interviewing"))
        elif kind == "cool":
            if i % 5 == 0:
                out.append(mail("outreach", 1 + i % 9, direction="in", sentiment=0.4, stage_hint="sourced",
                                category="recruiter_outreach"))
            else:
                out.append(mail("applied", 1 + (i * 7) % 14, **ack))
        elif kind == "cold":
            out.extend([mail("applied", 18 + i % 10, **ack)])
            if i % 3 == 0:
                out.append(meet("Screen", 22 + i % 6, "screening"))
        elif kind == "closed":
            out.extend([mail("applied", 8 + i % 18, **ack),
                        mail("update", 2 + i % 20, direction="in", sentiment=-0.3, cue="negative", stage_hint="closed-lost",
                             category="rejection", action_requested=False)])
        else:  # expired
            out.append(mail("applied", 31 + i, **ack))

    for kind, count in (("hot", 5), ("warm", 12), ("cool", 38), ("cold", 15), ("closed", 39), ("expired", 6)):
        for i in range(count):
            lead(kind, i)
    return out
