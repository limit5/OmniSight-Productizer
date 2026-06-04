# Cases 6+7 MAVLink transport CMake fragment (OP-2023).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-uav-mavlink-transport STATIC
  ${CMAKE_CURRENT_LIST_DIR}/mavlink-transport.cpp
  ${CMAKE_CURRENT_LIST_DIR}/mavlink-transport-udp.cpp
)

target_include_directories(omnisight-uav-mavlink-transport PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-uav-mavlink-transport PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-uav-mavlink-transport PRIVATE
  -Wall
  -Wextra
  -Werror
)
