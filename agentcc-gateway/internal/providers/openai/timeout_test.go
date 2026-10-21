package openai

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/futureagi/agentcc-gateway/internal/config"
	"github.com/futureagi/agentcc-gateway/internal/models"
)

// newSlowProvider returns a provider whose client timeout is shorter than the
// upstream takes to answer, so the provider ceiling is what ends the call.
func newSlowProvider(t *testing.T, upstreamDelay, clientTimeout time.Duration) *Provider {
	t.Helper()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(upstreamDelay)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(models.ChatCompletionResponse{ID: "late"})
	}))
	t.Cleanup(ts.Close)

	p, err := New("test-openai", config.ProviderConfig{
		BaseURL:        ts.URL,
		APIKey:         "test-key",
		DefaultTimeout: clientTimeout,
	})
	if err != nil {
		t.Fatalf("New() error: %v", err)
	}
	return p
}

func apiError(t *testing.T, err error) *models.APIError {
	t.Helper()
	if err == nil {
		t.Fatal("call succeeded, want a timeout error")
	}
	var apiErr *models.APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("error %v is not an *models.APIError", err)
	}
	return apiErr
}

// The provider's own client timeout never touches the request context, so a
// call it ends has to be read off the error. Reported as an upstream failure
// it becomes a 502, which the OpenAI and Anthropic SDKs both retry — on a
// request that already spent its whole budget.
func TestChatCompletion_ProviderCeilingIsAGatewayTimeout(t *testing.T) {
	p := newSlowProvider(t, 300*time.Millisecond, 50*time.Millisecond)

	// A request deadline far longer than the provider ceiling: ctx is still
	// live when the call is cut off, which is the whole point.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	_, err := p.ChatCompletion(ctx, &models.ChatCompletionRequest{Model: "gpt-4o"})

	apiErr := apiError(t, err)
	if apiErr.Status != http.StatusGatewayTimeout {
		t.Errorf("status = %d (%s / %s), want %d",
			apiErr.Status, apiErr.Type, apiErr.Code, http.StatusGatewayTimeout)
	}
	if apiErr.Type != models.ErrTypeGatewayTimeout {
		t.Errorf("type = %q, want %q", apiErr.Type, models.ErrTypeGatewayTimeout)
	}
}

// The same classification has to hold for a streamed call: its first error
// arrives on the error channel instead of the return value.
func TestStreamChatCompletion_ProviderCeilingIsAGatewayTimeout(t *testing.T) {
	p := newSlowProvider(t, 300*time.Millisecond, 50*time.Millisecond)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	_, errs := p.StreamChatCompletion(ctx, &models.ChatCompletionRequest{Model: "gpt-4o", Stream: true})

	select {
	case err := <-errs:
		if apiErr := apiError(t, err); apiErr.Status != http.StatusGatewayTimeout {
			t.Errorf("status = %d (%s), want %d", apiErr.Status, apiErr.Code, http.StatusGatewayTimeout)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("no error arrived on the stream error channel")
	}
}

// A transport failure that is not a timeout must stay an upstream error, or
// the fix above would relabel every unreachable provider as a timeout.
func TestChatCompletion_UnreachableProviderStaysAnUpstreamError(t *testing.T) {
	// A server that is closed at once: connecting fails immediately.
	ts := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := ts.URL
	ts.Close()

	p, err := New("test-openai", config.ProviderConfig{BaseURL: url, APIKey: "test-key"})
	if err != nil {
		t.Fatalf("New() error: %v", err)
	}

	_, err = p.ChatCompletion(context.Background(), &models.ChatCompletionRequest{Model: "gpt-4o"})

	if apiErr := apiError(t, err); apiErr.Status != http.StatusBadGateway {
		t.Errorf("status = %d (%s), want %d", apiErr.Status, apiErr.Code, http.StatusBadGateway)
	}
}

// A request whose own deadline expires keeps answering 504, as it did before.
func TestChatCompletion_RequestDeadlineIsStillAGatewayTimeout(t *testing.T) {
	p := newSlowProvider(t, 300*time.Millisecond, 30*time.Second)

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	_, err := p.ChatCompletion(ctx, &models.ChatCompletionRequest{Model: "gpt-4o"})

	if apiErr := apiError(t, err); apiErr.Status != http.StatusGatewayTimeout {
		t.Errorf("status = %d (%s), want %d", apiErr.Status, apiErr.Code, http.StatusGatewayTimeout)
	}
}
