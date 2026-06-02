# uvc-xu-dispatcher daemon skeleton (OP-1933).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_executable(uvc-xu-dispatcher
  ${CMAKE_CURRENT_LIST_DIR}/main.c
)

target_include_directories(uvc-xu-dispatcher PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(uvc-xu-dispatcher PRIVATE c_std_11)
target_compile_definitions(uvc-xu-dispatcher PRIVATE
  _POSIX_C_SOURCE=200809L
)
target_compile_options(uvc-xu-dispatcher PRIVATE
  -Wall
  -Wextra
  -Werror
)
