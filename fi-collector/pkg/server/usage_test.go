package server

import (
	"context"
	"encoding/hex"
	"testing"
	"time"

	chexp "github.com/future-agi/future-agi/fi-collector/exporter/clickhouse25exporter"
	"github.com/future-agi/future-agi/fi-collector/pkg/auth"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

func TestCountDistinctTracesEmpty(t *testing.T) {
	traces := ptrace.NewTraces()
	if n := countDistinctTraces(traces); n != 0 {
		t.Errorf("empty traces: got %d, want 0", n)
	}
}

func TestCountDistinctTracesSingle(t *testing.T) {
	traces := ptrace.NewTraces()
	rs := traces.ResourceSpans().AppendEmpty()
	ss := rs.ScopeSpans().AppendEmpty()
	span := ss.Spans().AppendEmpty()
	span.SetTraceID(pcommon.TraceID([16]byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}))

	if n := countDistinctTraces(traces); n != 1 {
		t.Errorf("single span: got %d, want 1", n)
	}
}

func TestCountDistinctTracesMultipleSpansSameTrace(t *testing.T) {
	traces := ptrace.NewTraces()
	rs := traces.ResourceSpans().AppendEmpty()
	ss := rs.ScopeSpans().AppendEmpty()
	tid := pcommon.TraceID([16]byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16})

	for i := 0; i < 5; i++ {
		span := ss.Spans().AppendEmpty()
		span.SetTraceID(tid)
		span.SetSpanID(pcommon.SpanID([8]byte{byte(i), 0, 0, 0, 0, 0, 0, 0}))
	}

	if n := countDistinctTraces(traces); n != 1 {
		t.Errorf("5 spans same trace: got %d, want 1", n)
	}
}

func TestCountDistinctTracesMultipleTraces(t *testing.T) {
	traces := ptrace.NewTraces()

	for i := 0; i < 3; i++ {
		rs := traces.ResourceSpans().AppendEmpty()
		ss := rs.ScopeSpans().AppendEmpty()
		span := ss.Spans().AppendEmpty()
		tid := [16]byte{}
		tid[0] = byte(i + 1)
		span.SetTraceID(pcommon.TraceID(tid))
	}

	if n := countDistinctTraces(traces); n != 3 {
		t.Errorf("3 distinct traces: got %d, want 3", n)
	}
}

func TestCountDistinctTracesDuplicatesAcrossResourceSpans(t *testing.T) {
	traces := ptrace.NewTraces()
	tid := pcommon.TraceID([16]byte{0xAA, 0xBB})

	// Same trace ID in two different ResourceSpans
	for i := 0; i < 2; i++ {
		rs := traces.ResourceSpans().AppendEmpty()
		ss := rs.ScopeSpans().AppendEmpty()
		span := ss.Spans().AppendEmpty()
		span.SetTraceID(tid)
		span.SetSpanID(pcommon.SpanID([8]byte{byte(i)}))
	}

	if n := countDistinctTraces(traces); n != 1 {
		t.Errorf("same trace across resource spans: got %d, want 1", n)
	}
}

func TestCheckUsageNilMetering(t *testing.T) {
	s := &Server{metering: nil}
	check, ok := s.checkUsage(context.Background())
	if !ok || !check.Allowed {
		t.Fatal("nil metering must return allowed=true")
	}
}

func TestCheckUsageNoAuthContext(t *testing.T) {
	s := &Server{metering: &auth.Metering{}}
	check, ok := s.checkUsage(context.Background())
	if !ok || !check.Allowed {
		t.Fatal("no auth context must return allowed=true")
	}
}

func TestEmitUsageNilUsage(t *testing.T) {
	s := &Server{usage: nil}
	// Must not panic
	s.emitUsage(context.Background(), ptrace.NewTraces(), 1024)
}

func TestEmitUsageNoAuthContext(t *testing.T) {
	s := &Server{usage: &auth.UsageEmitter{}}
	// Must not panic — no auth result in context
	s.emitUsage(context.Background(), ptrace.NewTraces(), 1024)
}

func TestCountDistinctTracesMultipleScopeSpans(t *testing.T) {
	traces := ptrace.NewTraces()
	rs := traces.ResourceSpans().AppendEmpty()

	tid1 := pcommon.TraceID([16]byte{1})
	tid2 := pcommon.TraceID([16]byte{2})

	ss1 := rs.ScopeSpans().AppendEmpty()
	ss1.Spans().AppendEmpty().SetTraceID(tid1)

	ss2 := rs.ScopeSpans().AppendEmpty()
	ss2.Spans().AppendEmpty().SetTraceID(tid2)

	ss3 := rs.ScopeSpans().AppendEmpty()
	ss3.Spans().AppendEmpty().SetTraceID(tid1) // duplicate

	if n := countDistinctTraces(traces); n != 2 {
		t.Errorf("2 traces across 3 scope spans: got %d, want 2", n)
	}
}

func TestDistinctTraceIDsReturnsSingleID(t *testing.T) {
	traces := ptrace.NewTraces()
	rs := traces.ResourceSpans().AppendEmpty()
	ss := rs.ScopeSpans().AppendEmpty()
	tid := [16]byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}
	// two spans, same trace -> one distinct id (the billing dedup key for a call)
	ss.Spans().AppendEmpty().SetTraceID(pcommon.TraceID(tid))
	ss.Spans().AppendEmpty().SetTraceID(pcommon.TraceID(tid))

	ids := distinctTraceIDs(traces)
	if len(ids) != 1 {
		t.Fatalf("got %d distinct ids, want 1", len(ids))
	}
	if ids[0] != tid {
		t.Errorf("distinct id = %x, want %x", ids[0], tid)
	}
}

func TestDistinctTraceIDsMultiple(t *testing.T) {
	traces := ptrace.NewTraces()
	for i := 0; i < 3; i++ {
		rs := traces.ResourceSpans().AppendEmpty()
		ss := rs.ScopeSpans().AppendEmpty()
		tid := [16]byte{}
		tid[0] = byte(i + 1)
		ss.Spans().AppendEmpty().SetTraceID(pcommon.TraceID(tid))
	}
	if got := len(distinctTraceIDs(traces)); got != 3 {
		t.Errorf("got %d distinct ids, want 3", got)
	}
}

const usageTestOrg = "33333333-3333-4333-8333-333333333333"

// recordedIngestion is one EmitIngestion call as recordingUsageEmitter saw it.
type recordedIngestion struct {
	orgID        string
	numTraces    int
	numSpans     int
	payloadBytes int64
	dedupKey     string
}

// recordingUsageEmitter captures the arguments of every EmitIngestion call.
type recordingUsageEmitter struct{ calls []recordedIngestion }

func (r *recordingUsageEmitter) EmitIngestion(orgID string, numTraces, numSpans int, payloadBytes int64, dedupKey string) {
	r.calls = append(r.calls, recordedIngestion{orgID, numTraces, numSpans, payloadBytes, dedupKey})
}

// buildUsagePayload builds one observe resource whose spans carry user.id and
// session.id. traceOf[i] picks span i's trace; the first span of a trace is its
// root. marked puts the simulation mark on the resource.
func buildUsagePayload(marked bool, traceOf []byte) ptrace.Traces {
	traces := ptrace.NewTraces()
	rs := traces.ResourceSpans().AppendEmpty()
	ra := rs.Resource().Attributes()
	ra.PutStr("service.name", "sim-agent")
	ra.PutStr("fi.project_id", "44444444-4444-4444-8444-444444444444")
	ra.PutStr("fi.org_id", usageTestOrg)
	ra.PutStr("project_type", "observe")
	if marked {
		ra.PutStr("fi.simulation.origin", "simulation")
	}
	ss := rs.ScopeSpans().AppendEmpty()
	roots := make(map[byte]pcommon.SpanID)
	for i, tr := range traceOf {
		sp := ss.Spans().AppendEmpty()
		sid := pcommon.SpanID([8]byte{0x51, byte(i + 1)})
		sp.SetTraceID(pcommon.TraceID([16]byte{0x61, tr}))
		sp.SetSpanID(sid)
		if root, ok := roots[tr]; ok {
			sp.SetParentSpanID(root)
		} else {
			roots[tr] = sid
		}
		sp.SetName("agent.turn")
		sp.SetStartTimestamp(pcommon.NewTimestampFromTime(time.Unix(1700000000, 0)))
		sp.SetEndTimestamp(pcommon.NewTimestampFromTime(time.Unix(1700000001, 0)))
		sp.Attributes().PutStr("user.id", "usage-user@example.test")
		sp.Attributes().PutStr("session.id", "usage-session-1")
	}
	return traces
}

// A simulated run's spans are billed exactly as any other: conversion leaves the
// payload alone and emitUsage counts the same traces, spans, bytes and key.
func TestEmitUsageCountsMarkedSpansAsToday(t *testing.T) {
	ctx := auth.WithResolvedContext(context.Background(), &auth.ResolveResult{OrgID: usageTestOrg}, "")
	single := [16]byte{0x61, 9}
	cases := []struct {
		name    string
		traceOf []byte
		want    recordedIngestion
	}{
		{"two traces three spans", []byte{1, 1, 2}, recordedIngestion{usageTestOrg, 2, 3, 1234, ""}},
		{"single trace", []byte{9, 9}, recordedIngestion{usageTestOrg, 1, 2, 1234, hex.EncodeToString(single[:])}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := &recordingUsageEmitter{}
			s := &Server{usage: rec}
			for _, marked := range []bool{true, false} {
				traces := buildUsagePayload(marked, tc.traceOf)
				before := traces.SpanCount()
				if before != len(tc.traceOf) {
					t.Fatalf("marked=%v: payload has %d spans, want %d", marked, before, len(tc.traceOf))
				}
				if _, _, err := chexp.ConvertWithIdentities(ctx, traces, nil); err != nil {
					t.Fatalf("marked=%v: convert: %v", marked, err)
				}
				if after := traces.SpanCount(); after != before {
					t.Fatalf("marked=%v: conversion changed the span count %d -> %d", marked, before, after)
				}
				s.emitUsage(ctx, traces, 1234)
			}
			if len(rec.calls) != 2 {
				t.Fatalf("EmitIngestion calls=%d want 2", len(rec.calls))
			}
			marked, unmarked := rec.calls[0], rec.calls[1]
			if marked != unmarked {
				t.Errorf("marked %+v != unmarked %+v", marked, unmarked)
			}
			if marked != tc.want {
				t.Errorf("marked %+v, want %+v", marked, tc.want)
			}
		})
	}
}
