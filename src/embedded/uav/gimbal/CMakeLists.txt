# Case 6/7 UAV gimbal PID controller CMake fragment (OP-2026).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-uav-gimbal-pid-controller STATIC
  ${CMAKE_CURRENT_LIST_DIR}/pid-controller.cpp
)

target_include_directories(omnisight-uav-gimbal-pid-controller PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-uav-gimbal-pid-controller PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-uav-gimbal-pid-controller PRIVATE
  -Wall
  -Wextra
  -Werror
)

add_executable(omnisight-uav-gimbal-pid-controller-smoke
  ${CMAKE_CURRENT_LIST_DIR}/pid-controller.cpp
)
target_compile_definitions(omnisight-uav-gimbal-pid-controller-smoke PRIVATE
  OMNISIGHT_GIMBAL_PID_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-gimbal-pid-controller-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-uav-gimbal-pid-controller-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
