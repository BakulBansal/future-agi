package common

import (
	"context"
	"errors"
	"net"
	"os"
)

// TimedOut reports whether a failed provider call ran out of time, rather than
// failing for a reason the upstream chose.
//
// Two clocks can end a call and only one of them shows up on ctx. The
// request's own deadline does. The provider's http.Client.Timeout does not: it
// aborts the call and leaves ctx untouched, so err is the only evidence the
// second clock expired. A caller that asks ctx alone therefore reads a
// provider ceiling as an upstream failure and answers 502 — a status every SDK
// retries, on a request that has already spent its whole budget.
func TimedOut(ctx context.Context, err error) bool {
	if ctx.Err() != nil {
		return true
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, os.ErrDeadlineExceeded) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}
