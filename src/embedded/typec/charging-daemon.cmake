# RK3588 userspace charging daemon (OP-2000).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_executable(charging-daemon
  ${CMAKE_CURRENT_LIST_DIR}/charging-daemon.c
)

target_include_directories(charging-daemon PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(charging-daemon PRIVATE c_std_11)
target_compile_definitions(charging-daemon PRIVATE
  _GNU_SOURCE
  _POSIX_C_SOURCE=200809L
)
target_compile_options(charging-daemon PRIVATE
  -Wall
  -Wextra
  -Werror
)
