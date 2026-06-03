# audio-router daemon skeleton (OP-1991).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_executable(audio-router-daemon
  ${CMAKE_CURRENT_LIST_DIR}/audio-router-daemon.c
  ${CMAKE_CURRENT_LIST_DIR}/../av-pair/probe_alsa_devices.c
)

target_include_directories(audio-router-daemon PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/../av-pair
)

target_compile_features(audio-router-daemon PRIVATE c_std_11)
target_compile_definitions(audio-router-daemon PRIVATE
  _POSIX_C_SOURCE=200809L
)
target_compile_options(audio-router-daemon PRIVATE
  -Wall
  -Wextra
  -Werror
)
