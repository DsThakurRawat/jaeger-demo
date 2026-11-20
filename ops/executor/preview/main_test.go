// Copyright (c) 2026 The Jaeger Authors.
// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestDeploymentStrategicMergePreservesExternalContainer(t *testing.T) {
	makeDeployment := func(containers string) json.RawMessage {
		return json.RawMessage(`{"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":"jaeger"},"spec":{"template":{"spec":{"containers":[` + containers + `]}}}}`)
	}
	old := `{"name":"jaeger","image":"old"}`
	desired := `{"name":"jaeger","image":"new"}`
	kind, patch, err := calculate(request{
		makeDeployment(old), makeDeployment(desired), makeDeployment(old + `,{"name":"external","image":"preserve"}`),
	})
	if err != nil || kind != "strategic" || !bytes.Contains(patch, []byte(desired)) && !bytes.Contains(patch, []byte(`"image":"new"`)) || bytes.Contains(patch, []byte("external")) {
		t.Fatalf("unexpected strategic merge result %q %s %v", kind, patch, err)
	}
}

func TestCustomResourceUsesTwoWayJSONMerge(t *testing.T) {
	old := json.RawMessage(`{"apiVersion":"monitoring.coreos.com/v1","kind":"ServiceMonitor","spec":{"one":1,"two":2}}`)
	next := json.RawMessage(`{"apiVersion":"monitoring.coreos.com/v1","kind":"ServiceMonitor","spec":{"one":1,"two":3}}`)
	kind, patch, err := calculate(request{old, next, old})
	if err != nil || kind != "merge" || string(patch) != `{"spec":{"two":3}}` {
		t.Fatalf("unexpected merge %q %s %v", kind, patch, err)
	}
}

func TestMalformedInputFailsSilently(t *testing.T) {
	var out bytes.Buffer
	for _, input := range []string{"{", `{"desired":"invalid-object"}`, `{"desired":{"kind":"","apiVersion":"v1"}}`, strings.Repeat("x", 16<<20+1)} {
		if run(strings.NewReader(input), &out) == nil || out.Len() != 0 {
			t.Fatal("invalid input produced a response")
		}
	}
}

func TestValidOutputTransport(t *testing.T) {
	obj := json.RawMessage(`{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"fixture"}}`)
	input, err := json.Marshal(request{obj, obj, obj})
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := run(bytes.NewReader(input), &out); err != nil || !strings.Contains(out.String(), `"type":"strategic"`) {
		t.Fatalf("invalid success output %s %v", out.String(), err)
	}
}
