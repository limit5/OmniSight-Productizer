# dag-executor smoke fixture (OP-1673).
#
# A deliberately minimal, host-native CMake project: one C source that
# links into a single executable named `firmware.bin`, emitted into the
# CMake binary dir. The dag-executor's cmake handler invokes
#   cmake -S . -B build  &&  cmake --build build
# (see backend/dag_executor.py LocalTaskHandler._command_sequence), so
# pinning RUNTIME_OUTPUT_DIRECTORY to ${CMAKE_BINARY_DIR} lands the artifact
# at build/firmware.bin — exactly the smoke DAG's compile.expected_output.
#
# NOT a real firmware image and NOT cross-compiled: this is the host-native
# smoke seed only. Cross-compile lives on the aarch64 DAG, out of scope here.
cmake_minimum_required(VERSION 3.20)
project(dag_smoke_fixture C)

add_executable(firmware.bin main.c)
set_target_properties(firmware.bin PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
