package auth

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// routingSchema is the part of the platform's tables these statements touch,
// written from the Django models with every column and its nullability and no
// column defaults (Django sets none), so a statement that leaves out a NOT
// NULL column fails here as it would there. It carries the unique index on a
// project's name and the unique constraint on its agent, and omits foreign
// keys to tables the statements do not read.
const routingSchema = `
CREATE TABLE accounts_workspace (
	id uuid PRIMARY KEY,
	name varchar(255) NOT NULL,
	display_name varchar(255) NOT NULL,
	description text NOT NULL,
	organization_id uuid NOT NULL,
	is_active boolean NOT NULL,
	is_default boolean NOT NULL,
	created_by_id uuid NOT NULL,
	created_at timestamptz NOT NULL,
	updated_at timestamptz NOT NULL,
	deleted boolean NOT NULL,
	deleted_at timestamptz NULL
);
CREATE UNIQUE INDEX unique_default_workspace_per_org ON accounts_workspace (organization_id, is_default) WHERE is_default;

CREATE TABLE simulate_agent_definition (
	id uuid PRIMARY KEY,
	organization_id uuid NOT NULL,
	workspace_id uuid NULL,
	deleted boolean NOT NULL,
	created_at timestamptz NOT NULL,
	updated_at timestamptz NOT NULL
);

CREATE TABLE accounts_orgapikey (
	id uuid PRIMARY KEY,
	name varchar(100) NOT NULL,
	api_key varchar(255) NOT NULL UNIQUE,
	secret_key varchar(255) NOT NULL,
	organization_id uuid NOT NULL,
	type varchar(50) NOT NULL,
	enabled boolean NOT NULL,
	user_id uuid NULL,
	workspace_id uuid NULL,
	created_at timestamptz NOT NULL,
	updated_at timestamptz NOT NULL,
	deleted boolean NOT NULL,
	deleted_at timestamptz NULL,
	simulation_agent_id uuid NULL
);

CREATE TABLE tracer_project (
	id uuid PRIMARY KEY,
	name varchar(255) NOT NULL,
	organization_id uuid NOT NULL,
	workspace_id uuid NULL,
	model_type varchar(50) NOT NULL,
	trace_type varchar(20) NOT NULL,
	metadata jsonb NULL,
	config jsonb NULL,
	session_config jsonb NULL,
	user_id uuid NULL,
	source varchar(20) NOT NULL,
	tags jsonb NOT NULL,
	created_at timestamptz NOT NULL,
	updated_at timestamptz NOT NULL,
	deleted boolean NOT NULL,
	deleted_at timestamptz NULL,
	simulation_agent_id uuid NULL UNIQUE REFERENCES simulate_agent_definition (id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX unique_project_per_org_type ON tracer_project (name, trace_type, organization_id, workspace_id) WHERE NOT deleted;
`

// routingDatabase creates a private database with routingSchema on the
// PostgreSQL named by OBS_TEST_PG_DSN (a loopback server with an explicit
// port), drops it when the test ends, and returns a pool on it.
func routingDatabase(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("OBS_TEST_PG_DSN")
	if dsn == "" {
		t.Skip("set OBS_TEST_PG_DSN for isolated PostgreSQL integration")
	}
	u, err := url.Parse(dsn)
	if err != nil || u.Hostname() != "127.0.0.1" || u.Port() == "" || u.Scheme != "postgres" {
		t.Fatal("test PostgreSQL must be loopback with explicit port")
	}
	ctx := context.Background()
	admin, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	database := fmt.Sprintf("routing_test_%d", time.Now().UnixNano())
	if _, err := admin.Exec(ctx, "CREATE DATABASE "+database); err != nil {
		admin.Close(ctx)
		t.Fatal(err)
	}
	u.Path = "/" + database
	pool, err := pgxpool.New(ctx, u.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Close()
		if _, err := admin.Exec(ctx, "DROP DATABASE "+database+" WITH (FORCE)"); err != nil {
			t.Error(err)
		}
		admin.Close(ctx)
	})
	if _, err := pool.Exec(ctx, routingSchema); err != nil {
		t.Fatal(err)
	}
	return pool
}

func routingResolver(pool *pgxpool.Pool, state columnState) *PGResolver {
	columns := newRoutingColumns(slog.Default(), nil)
	columns.set(state)
	return &PGResolver{read: pool, write: pool, columns: columns}
}

type routingFixture struct {
	t    *testing.T
	pool *pgxpool.Pool
}

func (f routingFixture) exec(sql string, args ...any) {
	f.t.Helper()
	if _, err := f.pool.Exec(context.Background(), sql, args...); err != nil {
		f.t.Fatalf("%v\n%s", err, sql)
	}
}

func (f routingFixture) workspace(org string, isDefault, isActive, deleted bool) string {
	id := uuid.NewString()
	f.exec(`INSERT INTO accounts_workspace VALUES ($1, $2, $2, '', $3, $4, $5, $6, now(), now(), $7, NULL)`,
		id, "ws-"+id[:8], org, isActive, isDefault, uuid.NewString(), deleted)
	return id
}

func (f routingFixture) agent(org string, workspace *string, deleted bool) string {
	id := uuid.NewString()
	f.exec(`INSERT INTO simulate_agent_definition VALUES ($1, $2, $3, $4, now(), now())`, id, org, workspace, deleted)
	return id
}

func (f routingFixture) key(keyType, org string, workspace, agent *string) (apiKey, secretKey string) {
	apiKey, secretKey = uuid.NewString(), uuid.NewString()
	f.exec(`INSERT INTO accounts_orgapikey VALUES ($1, 'k', $2, $3, $4, $5, true, NULL, $6, now(), now(), false, NULL, $7)`,
		uuid.NewString(), apiKey, secretKey, org, keyType, workspace, agent)
	return apiKey, secretKey
}

func (f routingFixture) count(sql string, args ...any) int {
	f.t.Helper()
	var n int
	if err := f.pool.QueryRow(context.Background(), sql, args...).Scan(&n); err != nil {
		f.t.Fatal(err)
	}
	return n
}

func TestValidateKeyReadsTheAgentOfAnIngestKey(t *testing.T) {
	pool := routingDatabase(t)
	f := routingFixture{t, pool}
	ctx := context.Background()
	org := uuid.NewString()
	ws := f.workspace(org, true, true, false)
	live := f.agent(org, &ws, false)
	softDeleted := f.agent(org, &ws, true)
	hardDeleted := uuid.NewString() // the key names it; no row exists

	read := func(r *PGResolver, apiKey, secretKey string) *ResolveResult {
		t.Helper()
		res, err := r.ValidateKey(ctx, apiKey, secretKey)
		if err != nil || res == nil {
			t.Fatalf("ValidateKey = %v, %v", res, err)
		}
		return res
	}

	with := routingResolver(pool, columnsPresent)
	api, secret := f.key("ingest", org, &ws, &live)
	res := read(with, api, secret)
	if res.KeyType != "ingest" || res.OrgID != org || res.WorkspaceID != ws || res.columnsMissing {
		t.Fatalf("result = %+v", res)
	}
	if res.SimulationAgent != (SimulationAgent{ID: live, OrgID: org}) {
		t.Fatalf("live agent = %+v", res.SimulationAgent)
	}

	api, secret = f.key("ingest", org, &ws, &softDeleted)
	if got := read(with, api, secret).SimulationAgent; got != (SimulationAgent{ID: softDeleted, OrgID: org, Deleted: true}) {
		t.Fatalf("soft-deleted agent = %+v", got)
	}

	// A hard-deleted agent leaves the key naming an id with no row: the key
	// still validates and the agent reads as deleted.
	api, secret = f.key("ingest", org, &ws, &hardDeleted)
	if got := read(with, api, secret).SimulationAgent; got != (SimulationAgent{ID: hardDeleted, Deleted: true}) {
		t.Fatalf("hard-deleted agent = %+v", got)
	}

	// A key that names no agent; and a key of another type, whose agent column
	// is ignored.
	api, secret = f.key("ingest", org, &ws, nil)
	if got := read(with, api, secret).SimulationAgent; got != (SimulationAgent{}) {
		t.Fatalf("key without agent = %+v", got)
	}
	api, secret = f.key("system", org, &ws, &live)
	if got := read(with, api, secret); got.SimulationAgent != (SimulationAgent{}) || got.KeyType != "system" {
		t.Fatalf("system key = %+v", got)
	}

	// A key with no workspace of its own takes the organization's default.
	api, secret = f.key("ingest", org, nil, &live)
	if got := read(with, api, secret); got.WorkspaceID != ws || got.SimulationAgent.ID != live {
		t.Fatalf("workspace-less key = %+v", got)
	}

	// Invalid and disabled keys are not found.
	if res, err := with.ValidateKey(ctx, "nope", "nope"); err != nil || res != nil {
		t.Fatalf("unknown key = %v, %v", res, err)
	}

	// Before the columns are known, or while one is missing, the statement of
	// the old schema is used and an ingest key says so.
	for _, state := range []columnState{columnsUnknown, columnsMissing} {
		without := routingResolver(pool, state)
		api, secret = f.key("ingest", org, &ws, &live)
		if got := read(without, api, secret); !got.columnsMissing || got.SimulationAgent != (SimulationAgent{}) {
			t.Fatalf("state %v: result = %+v", state, got)
		}
		api, secret = f.key("user", org, &ws, nil)
		if got := read(without, api, secret); got.columnsMissing {
			t.Fatalf("state %v: another key marked", state)
		}
	}
}

func TestValidateKeyFallsBackWhenTheColumnDisappears(t *testing.T) {
	pool := routingDatabase(t)
	f := routingFixture{t, pool}
	org := uuid.NewString()
	ws := f.workspace(org, true, true, false)
	agent := f.agent(org, &ws, false)
	api, secret := f.key("ingest", org, &ws, &agent)

	r := routingResolver(pool, columnsPresent)
	f.exec(`ALTER TABLE accounts_orgapikey DROP COLUMN simulation_agent_id`)
	res, err := r.ValidateKey(context.Background(), api, secret)
	if err != nil || res == nil {
		t.Fatalf("ValidateKey = %v, %v", res, err)
	}
	if !res.columnsMissing {
		t.Fatal("the key was not marked as read without the agent")
	}
	if r.columns.state() != columnsMissing {
		t.Fatalf("state = %v, want missing", r.columns.state())
	}
}

func TestRoutingColumnCheckSeesBothColumns(t *testing.T) {
	pool := routingDatabase(t)
	f := routingFixture{t, pool}
	r := routingResolver(pool, columnsUnknown)
	ctx := context.Background()

	if present, err := r.RoutingColumnsPresent(ctx); err != nil || !present {
		t.Fatalf("both columns: %v, %v", present, err)
	}
	f.exec(`ALTER TABLE accounts_orgapikey DROP COLUMN simulation_agent_id`)
	if present, err := r.RoutingColumnsPresent(ctx); err != nil || present {
		t.Fatalf("key column dropped: %v, %v", present, err)
	}
	f.exec(`ALTER TABLE accounts_orgapikey ADD COLUMN simulation_agent_id uuid NULL`)
	f.exec(`ALTER TABLE tracer_project DROP COLUMN simulation_agent_id`)
	if present, err := r.RoutingColumnsPresent(ctx); err != nil || present {
		t.Fatalf("project column dropped: %v, %v", present, err)
	}
	// A column of the same name in another schema does not count.
	f.exec(`CREATE SCHEMA other`)
	f.exec(`CREATE TABLE other.tracer_project (simulation_agent_id uuid)`)
	if present, err := r.RoutingColumnsPresent(ctx); err != nil || present {
		t.Fatalf("column in another schema: %v, %v", present, err)
	}

	canceled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := r.RoutingColumnsPresent(canceled); err == nil {
		t.Fatal("a failed check returned no error")
	}
}

type projectRow struct {
	ID, Name, Org, Source, TraceType, ModelType string
	Workspace, Agent                            *string
	UserSet, ConfigMatches, MetadataEmpty       bool
	SessionConfigEmpty, TagsEmpty, Deleted      bool
}

func (f routingFixture) projectOf(agent string) projectRow {
	f.t.Helper()
	var p projectRow
	err := f.pool.QueryRow(context.Background(), `
		SELECT id, name, organization_id, source, trace_type, model_type, workspace_id, simulation_agent_id,
		       user_id IS NOT NULL, config = $2::jsonb, metadata = '{}'::jsonb,
		       session_config = '[]'::jsonb, tags = '[]'::jsonb, deleted
		FROM tracer_project WHERE simulation_agent_id = $1`, agent, expectedProjectConfig).Scan(
		&p.ID, &p.Name, &p.Org, &p.Source, &p.TraceType, &p.ModelType, &p.Workspace, &p.Agent,
		&p.UserSet, &p.ConfigMatches, &p.MetadataEmpty, &p.SessionConfigEmpty, &p.TagsEmpty, &p.Deleted)
	if err != nil {
		f.t.Fatal(err)
	}
	return p
}

func TestEnsureSimulationProject(t *testing.T) {
	pool := routingDatabase(t)
	f := routingFixture{t, pool}
	r := routingResolver(pool, columnsPresent)
	ctx := context.Background()
	org := uuid.NewString()
	defaultWS := f.workspace(org, true, true, false)
	ownWS := f.workspace(org, false, false, false) // an agent's own workspace is used as stored

	t.Run("inserts the row the platform would", func(t *testing.T) {
		agent := f.agent(org, &ownWS, false)
		got, err := r.EnsureSimulationProject(ctx, org, agent)
		if err != nil {
			t.Fatal(err)
		}
		if !got.Created || got.Name != hiddenNamePrefix+agent || got.WorkspaceID != ownWS {
			t.Fatalf("project = %+v", got)
		}
		row := f.projectOf(agent)
		if row.ID != got.ID || row.Name != got.Name || row.Org != org || row.Source != "simulator" ||
			row.TraceType != "observe" || row.ModelType != "GenerativeLLM" || row.UserSet || row.Deleted ||
			!row.ConfigMatches || !row.MetadataEmpty || !row.SessionConfigEmpty || !row.TagsEmpty ||
			row.Workspace == nil || *row.Workspace != ownWS {
			t.Fatalf("row = %+v", row)
		}
	})

	t.Run("is idempotent", func(t *testing.T) {
		agent := f.agent(org, &ownWS, false)
		first, err := r.EnsureSimulationProject(ctx, org, agent)
		if err != nil || !first.Created {
			t.Fatalf("first = %+v, %v", first, err)
		}
		second, err := r.EnsureSimulationProject(ctx, org, agent)
		if err != nil || second.Created || second.ID != first.ID {
			t.Fatalf("second = %+v, %v", second, err)
		}
		if n := f.count(`SELECT count(*) FROM tracer_project WHERE simulation_agent_id = $1`, agent); n != 1 {
			t.Fatalf("%d rows", n)
		}
	})

	t.Run("two collectors racing end with one row", func(t *testing.T) {
		agent := f.agent(org, &ownWS, false)
		var wg sync.WaitGroup
		results := make([]SimulationProject, 8)
		errs := make([]error, len(results))
		for i := range results {
			wg.Add(1)
			go func() {
				defer wg.Done()
				results[i], errs[i] = r.EnsureSimulationProject(ctx, org, agent)
			}()
		}
		wg.Wait()
		created := 0
		for i := range results {
			if errs[i] != nil {
				t.Fatalf("racer %d: %v", i, errs[i])
			}
			if results[i].ID != results[0].ID {
				t.Fatalf("racers got different projects")
			}
			if results[i].Created {
				created++
			}
		}
		if created != 1 {
			t.Fatalf("%d racers report an insert, want 1", created)
		}
		if n := f.count(`SELECT count(*) FROM tracer_project WHERE simulation_agent_id = $1`, agent); n != 1 {
			t.Fatalf("%d rows", n)
		}
	})

	t.Run("an agent without a workspace gets the one active default", func(t *testing.T) {
		agent := f.agent(org, nil, false)
		got, err := r.EnsureSimulationProject(ctx, org, agent)
		if err != nil || got.WorkspaceID != defaultWS {
			t.Fatalf("project = %+v, %v", got, err)
		}
	})

	t.Run("no usable default workspace writes no row", func(t *testing.T) {
		for name, c := range map[string]struct{ isActive, deleted bool }{
			"inactive default": {isActive: false, deleted: false},
			"deleted default":  {isActive: true, deleted: true},
		} {
			otherOrg := uuid.NewString()
			f.workspace(otherOrg, true, c.isActive, c.deleted)
			agent := f.agent(otherOrg, nil, false)
			if _, err := r.EnsureSimulationProject(ctx, otherOrg, agent); !errors.Is(err, ErrSimulationNoWorkspace) {
				t.Fatalf("%s: err = %v", name, err)
			}
			if n := f.count(`SELECT count(*) FROM tracer_project WHERE simulation_agent_id = $1`, agent); n != 0 {
				t.Fatalf("%s: %d rows written", name, n)
			}
		}
		noWorkspaceOrg := uuid.NewString()
		agent := f.agent(noWorkspaceOrg, nil, false)
		if _, err := r.EnsureSimulationProject(ctx, noWorkspaceOrg, agent); !errors.Is(err, ErrSimulationNoWorkspace) {
			t.Fatalf("organization without a workspace: err = %v", err)
		}
		if n := f.count(`SELECT count(*) FROM tracer_project WHERE organization_id = $1`, noWorkspaceOrg); n != 0 {
			t.Fatalf("%d rows written", n)
		}
	})

	t.Run("a deleted, absent or foreign agent has no project", func(t *testing.T) {
		soft := f.agent(org, &ownWS, true)
		live := f.agent(org, &ownWS, false)
		for name, c := range map[string]struct{ org, agent string }{
			"soft-deleted":         {org, soft},
			"absent":               {org, uuid.NewString()},
			"agent of another org": {uuid.NewString(), live},
		} {
			if _, err := r.EnsureSimulationProject(ctx, c.org, c.agent); !errors.Is(err, ErrSimulationAgentDeleted) {
				t.Fatalf("%s: err = %v", name, err)
			}
		}
		if n := f.count(`SELECT count(*) FROM tracer_project WHERE simulation_agent_id IN ($1, $2)`, soft, live); n != 0 {
			t.Fatalf("%d rows written", n)
		}
	})

	t.Run("a deleted hidden project is not revived", func(t *testing.T) {
		agent := f.agent(org, &ownWS, false)
		if _, err := r.EnsureSimulationProject(ctx, org, agent); err != nil {
			t.Fatal(err)
		}
		f.exec(`UPDATE tracer_project SET deleted = true WHERE simulation_agent_id = $1`, agent)
		if _, err := r.EnsureSimulationProject(ctx, org, agent); !errors.Is(err, ErrSimulationAgentDeleted) {
			t.Fatalf("err = %v", err)
		}
		if row := f.projectOf(agent); !row.Deleted {
			t.Fatal("the deleted project was revived")
		}
	})

	t.Run("a project of the hidden name made by anything else is a conflict", func(t *testing.T) {
		agent := f.agent(org, &ownWS, false)
		f.exec(`INSERT INTO tracer_project (id, name, organization_id, workspace_id, model_type, trace_type, source, tags, deleted, created_at, updated_at)
			VALUES ($1, $2, $3, $4, 'GenerativeLLM', 'observe', 'prototype', '[]', false, now(), now())`,
			uuid.NewString(), hiddenNamePrefix+agent, org, ownWS)
		_, err := r.EnsureSimulationProject(ctx, org, agent)
		if !errors.Is(err, ErrSimulationNameConflict) {
			t.Fatalf("err = %v", err)
		}
		if n := f.count(`SELECT count(*) FROM tracer_project WHERE name = $1`, hiddenNamePrefix+agent); n != 1 {
			t.Fatalf("%d rows of that name", n)
		}
	})

	t.Run("an existing project keeps its workspace when the agent is moved", func(t *testing.T) {
		agent := f.agent(org, &ownWS, false)
		first, err := r.EnsureSimulationProject(ctx, org, agent)
		if err != nil || first.WorkspaceID != ownWS {
			t.Fatalf("first = %+v, %v", first, err)
		}
		movedWS := f.workspace(org, false, true, false)
		f.exec(`UPDATE simulate_agent_definition SET workspace_id = $1 WHERE id = $2`, movedWS, agent)
		second, err := r.EnsureSimulationProject(ctx, org, agent)
		if err != nil {
			t.Fatal(err)
		}
		if second.Created || second.ID != first.ID || second.WorkspaceID != ownWS || second.OrganizationID != org {
			t.Fatalf("second = %+v, want the project in %s", second, ownWS)
		}
		if row := f.projectOf(agent); row.Workspace == nil || *row.Workspace != ownWS {
			t.Fatalf("the project moved to %v", row.Workspace)
		}
	})

	t.Run("an existing project survives the loss of the default workspace", func(t *testing.T) {
		o := uuid.NewString()
		def := f.workspace(o, true, true, false)
		agent := f.agent(o, nil, false)
		first, err := r.EnsureSimulationProject(ctx, o, agent)
		if err != nil || !first.Created || first.WorkspaceID != def {
			t.Fatalf("first = %+v, %v", first, err)
		}
		f.exec(`UPDATE accounts_workspace SET is_active = false WHERE id = $1`, def)
		second, err := r.EnsureSimulationProject(ctx, o, agent)
		if err != nil {
			t.Fatalf("a project that exists is not a missing workspace: %v", err)
		}
		if second.Created || second.ID != first.ID || second.WorkspaceID != def {
			t.Fatalf("second = %+v, want the project in %s", second, def)
		}
		// Its deletion is still told apart from a missing workspace.
		f.exec(`UPDATE tracer_project SET deleted = true WHERE simulation_agent_id = $1`, agent)
		if _, err := r.EnsureSimulationProject(ctx, o, agent); !errors.Is(err, ErrSimulationAgentDeleted) {
			t.Fatalf("deleted project: err = %v", err)
		}
	})

	t.Run("a deleted agent with no workspace is deleted, not workspaceless", func(t *testing.T) {
		o := uuid.NewString()
		agent := f.agent(o, nil, true)
		if _, err := r.EnsureSimulationProject(ctx, o, agent); !errors.Is(err, ErrSimulationAgentDeleted) {
			t.Fatalf("err = %v", err)
		}
		if n := f.count(`SELECT count(*) FROM tracer_project WHERE organization_id = $1`, o); n != 0 {
			t.Fatalf("%d rows written", n)
		}
	})

	t.Run("a database error is not a refusal of the agent", func(t *testing.T) {
		agent := f.agent(org, &ownWS, false)
		canceled, cancel := context.WithCancel(ctx)
		cancel()
		_, err := r.EnsureSimulationProject(canceled, org, agent)
		if err == nil || errors.Is(err, ErrSimulationAgentDeleted) || errors.Is(err, ErrSimulationNoWorkspace) || errors.Is(err, ErrSimulationNameConflict) {
			t.Fatalf("err = %v", err)
		}
	})
}
