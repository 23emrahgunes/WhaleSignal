package storage

import "pm-edge/internal/engine"

// InsertSignalWithMicro persists the production Model-A signal first. The deep
// microstructure row is research-only: if that secondary write ever fails, it
// must never suppress the established paper entry/hedge path for the same
// evaluation. Microstructure schema/round-trip correctness is covered by its
// dedicated storage tests and the read-only research API exposes missing rows.
func (d *Database) InsertSignalWithMicro(r *engine.EvaluationResult) error {
	if err := d.InsertSignal(r); err != nil {
		return err
	}
	_ = d.InsertMicrostructureSnapshot(r)
	return nil
}
