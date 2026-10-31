"""The workers serve every workspace (app/workspaces.py): each loop runs each workspace in its own
scope and isolates failures; Temporal ids and inputs are per workspace with the owner's unchanged;
advisory locks are per workspace; a guest's block never dials; the owner's server files and
environment never reach a guest's work. Every address and name is made up."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app import calendar_sync, interview_types, lead_worker, notifications, owner, tenant, workspaces
from app.callback_workflow import DialSlotRequest, PlaceCallInput, workflow_runner
from app.callback_workflow import TASK_QUEUE as CALLBACK_QUEUE
from app.interview_workflow import (TASK_QUEUE, BlockCallWorkflow, BlockInput, BlockRecord, HeadsUpInput,
                                    PlanInput, SlotFinish, SlotInput, SyncInput)
from app.lead_workflow import GmailSyncInput

GUEST = "u_" + "a" * 20
OTHER = "u_" + "b" * 20


@pytest.fixture(autouse=True)
def clean():
    workspaces.forget()
    owner.reset()
    yield
    workspaces.forget()
    owner.reset()


# Ids and locks.

def test_the_owner_keeps_his_ids_and_locks_and_each_guest_gets_their_own():
    assert workspaces.scoped_id("valr-gmail-sync", tenant.OWNER) == "valr-gmail-sync"
    assert workspaces.scoped_id("valr-gmail-sync", None) == "valr-gmail-sync"  # No scope: the owner.
    assert workspaces.scoped_id("valr-gmail-sync", GUEST) == f"valr-gmail-sync-{GUEST}"
    assert workspaces.lock_key(7_202_610_18, tenant.OWNER) == 7_202_610_18
    keys = {workspaces.lock_key(7_202_610_18, user) for user in (tenant.OWNER, GUEST, OTHER)}
    assert len(keys) == 3 and all(-2**63 <= key < 2**63 for key in keys)
    assert workspaces.lock_key(7_202_610_17, GUEST) != workspaces.lock_key(7_202_610_18, GUEST)
    with tenant.as_user(GUEST):
        assert workspaces.scoped_id("valr-calendar-sync") == f"valr-calendar-sync-{GUEST}"
        assert workspaces.lock_key(7_202_610_31) == workspaces.lock_key(7_202_610_31, GUEST)


def test_slot_and_block_ids_are_per_workspace_and_the_owners_are_unchanged():
    key, start = "evt123_20261102T100000Z", "2026-11-02T10:00:00+00:00"
    with tenant.as_user(tenant.OWNER):
        owner_slot, owner_block = interview_types.slot_workflow_id(key), interview_types.block_id_for(start)
    assert owner_slot == interview_types.slot_workflow_id(key)  # Unscoped code: the owner's, as before.
    assert owner_block == "briefing-20261102T1000Z"
    with tenant.as_user(GUEST):
        guest_slot, guest_block = interview_types.slot_workflow_id(key), interview_types.block_id_for(start)
    with tenant.as_user(OTHER):
        other_slot = interview_types.slot_workflow_id(key)
    assert len({owner_slot, guest_slot, other_slot}) == 3  # One shared invite, three workflows.
    assert guest_slot.startswith("slot-") and len(guest_slot) == len(owner_slot)  # Page refs keep their shape.
    assert guest_block == f"briefing-20261102T1000Z-{GUEST}"


def test_workflow_inputs_carry_the_workspace_and_old_inputs_are_the_owners():
    event = interview_types.SlotEvent("k", "Interview", "2026-11-02T10:00:00+00:00", "2026-11-02T11:00:00+00:00")
    assert SlotInput(event, "owner", "b", "2026-11-02T09:00:00+00:00").user == tenant.OWNER
    assert BlockInput("b", "2026-11-02T10:00:00+00:00", ["k"], "owner", "", "x").user == tenant.OWNER
    assert SyncInput("sweep").user == GmailSyncInput("poll").user == tenant.OWNER
    for made in (PlanInput(event, "k", [], [], [], False, 0.4), HeadsUpInput("b", "s", [], "l"),
                 BlockRecord("b", "s", [], "l", "waiting"), SlotFinish("j", "done", 1.0, {}, "")):
        assert made.user == tenant.OWNER  # A payload from before workspaces decodes as the owner's.


def test_sync_requests_and_schedules_name_the_workspace(monkeypatch):
    from app import interviews, research_worker
    monkeypatch.setenv("VALR_LEADS", "1")
    monkeypatch.setenv("VALR_GMAIL_TOPIC", "projects/p/topics/t")
    with tenant.as_user(GUEST):
        request, workflow_id = interviews.sync_request("push")
        gmail, gmail_id = lead_worker.sync_request("push", 9)
    assert (request.user, workflow_id) == (GUEST, f"valr-calendar-sync-{GUEST}")
    assert (gmail.user, gmail.history_id, gmail_id) == (GUEST, 9, f"valr-gmail-sync-{GUEST}")
    with tenant.as_user(tenant.OWNER):
        assert interviews.sync_request("push")[1] == "valr-calendar-sync"
        assert lead_worker.sync_request("push")[1] == "valr-gmail-sync"
    owner_plans = lead_worker.lead_schedules(tenant.OWNER)
    assert [name for name, _, _ in owner_plans] == ["valr-gmail-poll", "valr-gmail-watch"]
    assert [action.id for _, action, _ in owner_plans] == ["valr-gmail-poll-run", "valr-gmail-watch-run"]
    assert owner_plans[1][1].args == ["schedule"]  # As the owner's schedule was created before.
    guest_plans = lead_worker.lead_schedules(GUEST)
    assert [name for name, _, _ in guest_plans] == [f"valr-gmail-poll-{GUEST}", f"valr-gmail-watch-{GUEST}"]
    assert guest_plans[0][1].args[0].user == GUEST and guest_plans[1][1].args == ["schedule", GUEST]
    assert research_worker.sweep_schedule(tenant.OWNER)[0] == "valr-calendar-sweep"
    assert research_worker.sweep_schedule(tenant.OWNER)[1].id == "valr-calendar-sweep-run"
    name, action = research_worker.sweep_schedule(GUEST)
    assert name == f"valr-calendar-sweep-{GUEST}" and action.args[0].user == GUEST
    assert set(workspaces.schedule_ids(GUEST)) == {f"valr-calendar-sweep-{GUEST}", f"valr-gmail-poll-{GUEST}",
                                                   f"valr-gmail-watch-{GUEST}"}


def test_ensure_schedules_serves_each_connected_workspace_once(monkeypatch):
    from app import research_worker
    made: list[tuple[str, str]] = []

    async def connected(connect):
        return [tenant.OWNER, GUEST]

    async def sweep(client, user):
        made.append(("sweep", user))
        if user == GUEST and len([m for m in made if m[1] == GUEST]) == 1:
            raise RuntimeError("Temporal blip")  # Tried again next pass; the owner is served.

    async def leads(client, user):
        made.append(("leads", user))
    monkeypatch.setenv("VALR_BRIEFINGS", "1")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(workspaces, "connected", connected)
    monkeypatch.setattr(research_worker, "ensure_sweep", sweep)
    monkeypatch.setattr(lead_worker, "ensure_lead_schedules", leads)
    done: set[str] = set()
    assert asyncio.run(research_worker.ensure_schedules(object(), done)) == [tenant.OWNER]
    assert asyncio.run(research_worker.ensure_schedules(object(), done)) == [GUEST]
    assert asyncio.run(research_worker.ensure_schedules(object(), done)) == []
    assert made == [("sweep", tenant.OWNER), ("leads", tenant.OWNER), ("sweep", GUEST), ("sweep", GUEST),
                    ("leads", GUEST)]


# The loop helper.

def test_each_runs_every_workspace_in_its_own_scope_and_isolates_failures(caplog):
    seen: dict[str, str | None] = {}
    running, peak = [0], [0]

    async def work(user):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.01)
        seen[user] = tenant.get()
        running[0] -= 1
        if user == GUEST:
            raise ValueError("this workspace's Gmail is broken")
        if user == OTHER:
            await asyncio.sleep(5)  # Past its budget.
        return "skipped" if user == "u_" + "c" * 20 else None

    users = [tenant.OWNER, GUEST, OTHER, "u_" + "c" * 20, "u_" + "d" * 20]
    with tenant.as_user(None):
        result = asyncio.run(workspaces.each(work, label="test", only=users, concurrency=2, budget_s=0.2))
        assert tenant.get() is None  # The caller's scope is untouched.
    assert result == {tenant.OWNER: "ok", GUEST: "failed", OTHER: "timeout", "u_" + "c" * 20: "skipped",
                      "u_" + "d" * 20: "ok"}
    assert seen == {user: user for user in users}
    assert peak[0] <= 2
    assert "ValueError" in caplog.text and "Gmail is broken" not in caplog.text  # Error type only.


class ListingConnection:
    def __init__(self, ids, table=True):
        self.ids, self.table = ids, table

    async def execute(self, sql, params=()):
        rows = [(self.table,)] if "to_regclass" in sql else [(user,) for user in self.ids]

        class Cursor:
            async def fetchone(self):
                return rows[0]

            async def fetchall(self):
                return rows
        return Cursor()


def test_the_listing_puts_the_owner_first_and_always_has_him():
    assert asyncio.run(workspaces.listing(ListingConnection([GUEST, tenant.OWNER, OTHER]))) == [
        tenant.OWNER, GUEST, OTHER]
    assert asyncio.run(workspaces.listing(ListingConnection([GUEST]))) == [tenant.OWNER, GUEST]
    assert asyncio.run(workspaces.listing(ListingConnection([], table=False))) == [tenant.OWNER]

    async def down():
        raise OSError("no database")
    assert asyncio.run(workspaces.users(down)) == [tenant.OWNER]


# The loops.

class Connections:
    """connect() for the loops: records the scope each connection was opened in."""

    def __init__(self):
        self.scopes: list[str | None] = []

    async def __call__(self):
        self.scopes.append(tenant.get())

        class Connection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=()):
                rows = [(True,)] if "to_regclass" in sql else [(tenant.OWNER,), (GUEST,), (OTHER,)]

                class Cursor:
                    async def fetchone(self):
                        return rows[0]

                    async def fetchall(self):
                        return rows
                return Cursor()
        return Connection()


def test_the_notifications_loop_dispatches_each_workspace_in_its_scope(monkeypatch):
    dispatched: list[str | None] = []
    stop = asyncio.Event()

    async def run_dispatch(connection, now=None):
        dispatched.append(tenant.get())
        if tenant.get() == GUEST:
            raise RuntimeError("one bad workspace")
        if len(dispatched) >= 3:
            stop.set()
        return {"pushed": 0}

    async def latency(store, now):
        assert tenant.get() == tenant.OWNER  # The SLA figures stay the owner's.
    monkeypatch.setattr(notifications, "run_dispatch", run_dispatch)
    monkeypatch.setattr(notifications, "log_latency", latency)
    monkeypatch.setenv("VALR_NOTIFY_LOOP_S", "0.01")
    asyncio.run(asyncio.wait_for(notifications.loop(Connections(), stop), 5))
    assert sorted(dispatched[:3]) == sorted([tenant.OWNER, GUEST, OTHER])  # The failure stopped no one.


def test_the_gmail_check_ticks_each_connected_workspace_and_starts_its_own_sync(monkeypatch):
    started: list[tuple[str, str | None]] = []
    stop = asyncio.Event()

    async def connected(connect):
        return [tenant.OWNER, GUEST]

    async def tick(self):
        if tenant.get() == GUEST:
            await self.start("check")
        if len(ticks) >= 1:
            stop.set()
        ticks.append(tenant.get())
        return "idle"
    ticks: list[str | None] = []

    async def start(reason):
        started.append((reason, tenant.get()))
    monkeypatch.setenv("VALR_LEADS", "1")
    monkeypatch.setenv("VALR_GMAIL_CHECK_S", "0.01")
    monkeypatch.setattr(workspaces, "connected", connected)
    monkeypatch.setattr(lead_worker.GmailCheck, "tick", tick)
    asyncio.run(asyncio.wait_for(lead_worker.check_loop(Connections(), start, stop), 5))
    assert sorted(ticks[:2]) == sorted([tenant.OWNER, GUEST])
    assert started == [("check", GUEST)]  # Started in the guest's scope: their sync, their id.


def test_the_profile_refresh_loads_every_workspace(monkeypatch):
    refreshed: list[str | None] = []
    stop = asyncio.Event()

    async def refresh(connection=None, force=False):
        refreshed.append(tenant.get())
        if len(refreshed) >= 3:
            stop.set()
    monkeypatch.setattr(owner, "refresh", refresh)
    asyncio.run(asyncio.wait_for(owner.keep_fresh(stop, every=0.01, connect=Connections()), 5))
    assert sorted(refreshed[:3]) == sorted([tenant.OWNER, GUEST, OTHER])


# Activities.

class ActivityConnection:
    """A connection whose statements are recorded with the scope they ran in."""

    def __init__(self, log, rows=None):
        self.log, self.rows = log, rows or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        self.log.append((tenant.get(), sql, params))
        rows = next((value for key, value in self.rows.items() if key in sql), [])

        class Cursor:
            async def fetchone(self):
                return rows[0] if rows else None

            async def fetchall(self):
                return rows
        return Cursor()


def test_calendar_sync_runs_in_its_workspace_under_that_workspaces_lock(monkeypatch):
    from app import interview_store, interview_worker
    log: list = []
    rows = {"FROM valr_users": [("guest@elsewhere.example",)], "FROM oauth_tokens": [("guest@elsewhere.example",)]}

    async def connect():
        return ActivityConnection(log, rows)

    async def nothing(connection):
        return None

    async def sync(self, connection, request, now):
        assert tenant.get() == GUEST and interview_worker.owner_emails() == {"guest@elsewhere.example"}
        assert interview_worker.owner_key() == f"workspace:{GUEST}"
        return "synced"
    monkeypatch.setattr(interview_worker, "connect", connect)
    monkeypatch.setattr(calendar_sync, "setup", nothing)
    monkeypatch.setattr(interview_store, "setup", nothing)
    monkeypatch.setattr(interview_worker.InterviewActivities, "sync", sync)
    monkeypatch.setenv("VALR_OWNER_EMAILS", "owner@example.com")
    activities = interview_worker.InterviewActivities(client=None, http=object())
    assert asyncio.run(activities.calendar_sync(SyncInput("push", user=GUEST))) == "synced"
    locks = [(scope, params) for scope, sql, params in log if "advisory" in sql]
    key = workspaces.lock_key(interview_worker.SYNC_LOCK, GUEST)
    assert locks == [(GUEST, (key,)), (GUEST, (key,))]  # Lock and unlock: this workspace's key.
    assert all(scope == GUEST for scope, _, _ in log)
    log.clear()

    async def owner_sync(self, connection, request, now):
        assert tenant.get() == tenant.OWNER and interview_worker.owner_emails() == {"owner@example.com"}
        return "synced"
    monkeypatch.setattr(interview_worker.InterviewActivities, "sync", owner_sync)
    asyncio.run(activities.calendar_sync(SyncInput("sweep")))  # From before workspaces: the owner's.
    assert [params for _, sql, params in log if "advisory" in sql][0] == (interview_worker.SYNC_LOCK,)


def test_a_guest_block_starts_with_no_phone_and_no_dial(monkeypatch):
    from app import interview_worker
    started = []

    class Client:
        async def start_workflow(self, run, request, **options):
            started.append((request, options["id"]))
    monkeypatch.setenv("VALR_BRIEFING_DIAL", "1")
    monkeypatch.setenv("VALR_OWNER_PHONE", "+15550100001")
    activities = interview_worker.InterviewActivities(Client(), http=object())
    with tenant.as_user(GUEST):
        block = interview_types.BlockPlan(interview_types.block_id_for("2026-11-02T10:00:00+00:00"),
                                          "2026-11-02T10:00:00+00:00", "2026-11-02T11:00:00+00:00", ["k"])
        asyncio.run(activities.start_block(block, "your 3:30 PM interview"))
    with tenant.as_user(tenant.OWNER):
        asyncio.run(activities.start_block(interview_types.BlockPlan(
            "briefing-20261102T1000Z", "2026-11-02T10:00:00+00:00", "2026-11-02T11:00:00+00:00", ["k"]), "x"))
    (guest, guest_id), (mine, mine_id) = started
    assert (guest.user, guest.dial, guest.phone_e164) == (GUEST, False, "")
    assert guest_id.endswith(GUEST) and mine_id == "briefing-20261102T1000Z"
    assert (mine.user, mine.dial, mine.phone_e164) == (tenant.OWNER, True, "+15550100001")
    assert not asyncio.run(activities.briefing_auto_dial(GUEST))  # Never an automatic call for a guest.


def test_a_guest_never_gets_the_owners_files_or_environment(monkeypatch, tmp_path):
    from app import company_context, company_names, interview_worker, notetakers, phone_recordings, private_sources
    perf = tmp_path / "performance.json"
    perf.write_text('{"interviews": [{"company": "Secretco", "date": "2026-10-01"}]}')
    monkeypatch.setenv("VALR_PERFORMANCE_PATH", str(perf))
    monkeypatch.setenv("FATHOM_API_KEY", "owner-fathom-key")
    monkeypatch.setenv("VALR_CANDIDATE_NAME", "Ownername Example")
    monkeypatch.setenv("VALR_OWNER_NAME", "Ownername Example")
    with tenant.as_user(tenant.OWNER):
        assert lead_worker.read_performance() is not None
        assert notetakers.env_key("fathom") == "owner-fathom-key"
        assert "ownername" in company_names.owner_words()
    with tenant.as_user(GUEST):
        assert lead_worker.read_performance() is None
        assert notetakers.env_key("fathom") == ""
        assert interview_worker.owner_context() == ("", "")
        assert not interview_worker.history_available()
        assert company_context.performance_reviews() == []
        assert private_sources.history("Secretco", ["Secretco"]) == ("", [])
        assert "ownername" not in company_names.owner_words()
        assert phone_recordings.candidate_name() == ""  # Their own Profile name (none yet).
        assert asyncio.run(lead_worker.LeadActivities(http=object()).fathom_touches(datetime.now(timezone.utc))) == []


def test_a_guests_calendar_channel_names_their_workspace(monkeypatch):
    import httpx
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"resourceId": "r1", "expiration": "1800000000000"})

    async def watch(workspace):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await calendar_sync.watch(http, "token", "https://valr.example/hooks/gcal",
                                             datetime.now(timezone.utc), workspace=workspace)
    guest = asyncio.run(watch(GUEST))
    mine = asyncio.run(watch(tenant.OWNER))
    old = asyncio.run(watch(None))
    assert calendar_sync.channel_workspace(guest["channel_id"]) == GUEST
    assert calendar_sync.channel_workspace(mine["channel_id"]) == tenant.OWNER
    assert calendar_sync.channel_workspace(old["channel_id"]) == tenant.OWNER
    assert len(guest["channel_id"]) <= 64  # Google's limit.
    for forged in ("valr-u_zz-0123456789abcdef", "valr-u_" + "a" * 20, None, "", "x" * 300):
        assert calendar_sync.channel_workspace(forged) == tenant.OWNER  # Then the owner's state must match.


# The block workflow in Temporal's time-skipping server.

def test_a_guest_block_never_dials_even_when_told_to():
    dials, heads_ups, scopes = [], [], []

    @activity.defn(name="slots_ready")
    async def slots_ready(keys: list[str], user: str = "owner") -> bool:
        return True

    @activity.defn(name="block_record")
    async def block_record(record: BlockRecord) -> None:
        scopes.append(record.user)

    @activity.defn(name="dials_paused")
    async def dials_paused(user: str = "owner") -> bool:
        scopes.append(user)
        return False

    @activity.defn(name="briefing_auto_dial")
    async def briefing_auto_dial(user: str = "owner") -> bool:
        scopes.append(user)
        return True  # Even if asked to dial, a guest's block never does.

    @activity.defn(name="push_heads_up")
    async def push_heads_up(request: HeadsUpInput) -> str:
        heads_ups.append(request.user)
        return "sent"

    @activity.defn(name="reserve_dial_slot")
    async def reserve_dial_slot(request: DialSlotRequest) -> bool:
        return True

    @activity.defn(name="place_call")
    async def place_call(request: PlaceCallInput) -> str:
        dials.append(request)
        return "call-1"

    async def main():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            start = (await env.get_current_time()) + timedelta(hours=2)
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[BlockCallWorkflow],
                              workflow_runner=workflow_runner(),
                              activities=[slots_ready, block_record, dials_paused, briefing_auto_dial, push_heads_up]), \
                    Worker(env.client, task_queue=CALLBACK_QUEUE, activities=[reserve_dial_slot, place_call]):
                status = await env.client.execute_workflow(
                    BlockCallWorkflow.run, BlockInput(f"briefing-x-{GUEST}", start.isoformat(), ["k"], f"workspace:{GUEST}",
                                                      "+15550100009", "your interview", dial=True, user=GUEST),
                    id=f"briefing-x-{GUEST}", task_queue=TASK_QUEUE)
        return status
    status = asyncio.run(main())
    assert dials == [] and status.state == "briefed" and status.detail == "dry run: not dialled"
    assert set(scopes) == {GUEST}  # Every activity ran in the guest's workspace.
