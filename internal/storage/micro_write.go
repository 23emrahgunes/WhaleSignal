package storage

import "pm-edge/internal/engine"

// InsertSignalWithMicro keeps the legacy signal row and the deep-microstructure
// research row aligned at the same evaluator timestamp. Model-A remains the
// production paper signal; Model-B is research-only.
func (d *Database) InsertSignalWithMicro(r *engine.EvaluationResult) error {
	if err := d.InsertSignal(r); err != nil {
		return err
	}
	if err := d.EnsureMicrostructureSchema(); err != nil {
		return err
	}
	return d.InsertMicrostructureSnapshot(r)
}
