package toolperm

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"

	"github.com/futureagi/agentcc-gateway/internal/guardrails"
	"github.com/futureagi/agentcc-gateway/internal/models"
)

func requestWithTools(t *testing.T, toolsJSON string) *models.ChatCompletionRequest {
	t.Helper()
	var req models.ChatCompletionRequest
	body := `{"model":"m","messages":[{"role":"user","content":"hi"}],"tools":` + toolsJSON + `}`
	if err := json.Unmarshal([]byte(body), &req); err != nil {
		t.Fatalf("reading the request: %v", err)
	}
	return &req
}

// This guardrail is configured independently of the tool-policy plugin, and
// read tool.Function.Name — so a provider-executed capability, which names no
// function, was invisible to it. A denylist for code_execution permitted both
// a bare and a typed entry, and the provider forwarded the capability.
func TestGuardrail_DeniesACapabilityInEitherSpelling(t *testing.T) {
	pairs := [][2]string{
		{"google_search", "googleSearch"},
		{"google_search_retrieval", "googleSearchRetrieval"},
		{"url_context", "urlContext"},
		{"code_execution", "codeExecution"},
	}

	for _, pair := range pairs {
		for _, configured := range pair {
			for _, requested := range pair {
				for _, shape := range []string{"bare", "typed"} {
					t.Run(fmt.Sprintf("deny %s / send %s / %s", configured, requested, shape), func(t *testing.T) {
						tools := `[{"` + requested + `":{}}]`
						if shape == "typed" {
							tools = `[{"type":"` + requested + `"}]`
						}

						g := New("toolperm", map[string]interface{}{
							"mode":     "denylist",
							"apply_to": "request",
							"tools":    configured,
						})

						res := g.Check(context.Background(), &guardrails.CheckInput{
							Request: requestWithTools(t, tools),
						})
						if res.Pass {
							t.Errorf("the denied capability passed the guardrail")
						}
					})
				}
			}
		}
	}
}

// An allowlist for an ordinary function must not be satisfied by a capability
// the caller smuggled in beside it.
func TestGuardrail_AllowlistDoesNotAdmitAContradictoryEntry(t *testing.T) {
	g := New("toolperm", map[string]interface{}{
		"mode":     "allowlist",
		"apply_to": "request",
		"tools":    "get_weather",
	})

	res := g.Check(context.Background(), &guardrails.CheckInput{
		Request: requestWithTools(t, `[{"type":"code_execution","function":{"name":"get_weather"}}]`),
	})
	if res.Pass {
		t.Error("the entry passed on its function name while code_execution is what travels")
	}
}

// An ordinary function name is untouched by the canonicalization.
func TestGuardrail_OrdinaryFunctionSemanticsAreUnchanged(t *testing.T) {
	g := New("toolperm", map[string]interface{}{
		"mode":     "allowlist",
		"apply_to": "request",
		"tools":    "get_weather,get_time",
	})

	res := g.Check(context.Background(), &guardrails.CheckInput{
		Request: requestWithTools(t, `[{"type":"function","function":{"name":"get_weather"}}]`),
	})
	if !res.Pass {
		t.Errorf("an allowed function was refused: %+v", res)
	}

	denied := g.Check(context.Background(), &guardrails.CheckInput{
		Request: requestWithTools(t, `[{"type":"function","function":{"name":"delete_everything"}}]`),
	})
	if denied.Pass {
		t.Error("a function outside the allowlist passed")
	}
}
