// Copyright (c) 2026 The Jaeger Authors.
// SPDX-License-Identifier: Apache-2.0

// Command jaeger-helm-preview computes a Helm 4.3.2 client-side patch offline.
// stdin holds original/desired/live JSON objects. No API or kubeconfig is used.
package main

import (
	"encoding/json"
	"errors"
	"io"
	"os"

	jsonpatch "github.com/evanphx/json-patch/v5"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/strategicpatch"
	"k8s.io/client-go/kubernetes/scheme"
)

type request struct {
	Original json.RawMessage
	Desired  json.RawMessage
	Live     json.RawMessage
}

func calculate(in request) (string, []byte, error) {
	var identity struct{ APIVersion, Kind string }
	if err := json.Unmarshal(in.Desired, &identity); err != nil {
		return "", nil, err
	}
	gv, err := schema.ParseGroupVersion(identity.APIVersion)
	if err != nil || identity.Kind == "" {
		return "", nil, errors.New("invalid identity")
	}
	target, err := scheme.Scheme.New(gv.WithKind(identity.Kind))
	if runtime.IsNotRegisteredError(err) {
		// Helm upgrade without take-ownership uses a two-way JSON merge for CRs.
		patch, err := jsonpatch.CreateMergePatch(in.Original, in.Desired)
		return "merge", patch, err
	}
	if err != nil {
		return "", nil, err
	}
	meta, err := strategicpatch.NewPatchMetaFromStruct(target)
	if err != nil {
		return "", nil, err
	}
	patch, err := strategicpatch.CreateThreeWayMergePatch(in.Original, in.Desired, in.Live, meta, true)
	return "strategic", patch, err
}

func run(in io.Reader, out io.Writer) error {
	const max = 16 << 20
	data, err := io.ReadAll(io.LimitReader(in, max+1))
	if err != nil || len(data) > max {
		return errors.New("oversized input")
	}
	var req request
	if err := json.Unmarshal(data, &req); err != nil {
		return err
	}
	kind, patch, err := calculate(req)
	if err != nil {
		return err
	}
	return json.NewEncoder(out).Encode(struct {
		Type  string          `json:"type"`
		Patch json.RawMessage `json:"patch"`
	}{kind, patch})
}

func main() {
	if run(os.Stdin, os.Stdout) != nil {
		// Errors from patch libraries can include full private manifests.
		os.Exit(1)
	}
}
