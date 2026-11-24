// Check the real manifest expressions with the CEL-Go version used by
// kustomize-controller v1.10.2 (Flux 2.10.1), without a live Kubernetes API.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
)

type testCase struct {
	Name       string         `json:"name"`
	Expression string         `json:"expression"`
	Input      map[string]any `json:"input"`
	Expected   bool           `json:"expected"`
}

func check(c testCase) error {
	// Flux parses health expressions without static declarations because CRDs
	// need not have the same top-level fields. These expressions need only the
	// standard macros; use the same optimizer and optional-type support.
	env, err := cel.NewEnv(cel.OptionalTypes())
	if err != nil {
		return err
	}
	ast, issues := env.Parse(c.Expression)
	if issues != nil && issues.Err() != nil {
		return issues.Err()
	}
	program, err := env.Program(ast, cel.EvalOptions(cel.OptOptimize))
	if err != nil {
		return err
	}
	value, _, err := program.Eval(c.Input)
	if err != nil {
		return err
	}
	result, ok := value.(types.Bool)
	if !ok || bool(result) != c.Expected {
		return fmt.Errorf("got %v, want %v", value, c.Expected)
	}
	return nil
}

func main() {
	var cases []testCase
	if err := json.NewDecoder(os.Stdin).Decode(&cases); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, c := range cases {
		if err := check(c); err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", c.Name, err)
			os.Exit(1)
		}
	}
	fmt.Printf("%d CEL-Go health cases passed\n", len(cases))
}
