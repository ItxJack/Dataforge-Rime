# Convenience only. `python run.py` is the canonical entry point and requires
# no make, which matters because judges may be on Windows.
.PHONY: all judge test chaos preflight demo speed live
all:       ; @python run.py
judge:     ; @python run.py judge
test:      ; @python run.py test
chaos:     ; @python run.py chaos
preflight: ; @python run.py preflight
demo:      ; @python run.py demo
speed:     ; @python run.py speed
live:      ; @python run.py live
