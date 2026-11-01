# cocotb testbench for feed_handler. Usually run from the repo root via `make test`.
#   make                  run every test with Verilator
#   make GROUP=filter     run one group: reset, counter, filter, decode, fcs, perf, random
#   make SIM=icarus       run the same tests with Icarus Verilog instead
#   make WAVES=1          also dump waveforms (Verilator: tb/dump.vcd, Icarus: tb/sim_build/feed_handler.fst)
#                         run `make clean` first whenever you switch WAVES on or off (the simulator has to be rebuilt)

SIM ?= verilator
TOPLEVEL_LANG = verilog
VERILOG_SOURCES = $(CURDIR)/../rtl/feed_handler.sv
COCOTB_TOPLEVEL = feed_handler
COCOTB_TEST_MODULES = test_feed_handler

ifdef GROUP
  export COCOTB_TEST_FILTER = test_$(GROUP)_    # tests are named test_<group>_...
endif

ifeq ($(SIM),verilator)
  EXTRA_ARGS += -Wno-fatal          # print Verilator warnings but keep going; `make lint` is strict
  ifeq ($(WAVES),1)
    VERILATOR_TRACE = 1
  endif
endif

include $(shell cocotb-config --makefiles)/Makefile.sim
