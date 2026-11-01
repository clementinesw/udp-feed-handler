# udp-feed-handler
#   make test               run every test (Verilator)
#   make test GROUP=filter  run one group of tests (reset, counter, filter, decode, fcs, perf, random)
#   make test SIM=icarus    use Icarus Verilog instead of Verilator
#   make test WAVES=1       dump waveforms too (make clean first when switching WAVES on or off)
#   make lint               strict Verilator lint (-Wall)
#   make synth              synthesise for AMD UltraScale+ with Yosys: resources + logic depth
#   make clean

YOSYS ?= yosys

.PHONY: test lint synth clean

test:
	$(MAKE) -C tb

lint:
	verilator --lint-only -Wall rtl/feed_handler.sv && echo "lint clean"

synth:
	$(YOSYS) -q -s synth/synth.ys
	@cat synth/report.txt

clean:
	$(MAKE) -C tb clean
	rm -rf tb/__pycache__ tb/results.xml tb/dump.vcd synth/report.txt
