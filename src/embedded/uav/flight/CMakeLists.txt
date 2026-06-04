# Cases 6+7 PX4 / ArduPilot autopilot abstraction CMake fragment (OP-2033).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-uav-autopilot-abstraction STATIC
  ${CMAKE_CURRENT_LIST_DIR}/autopilot-abstraction.cpp
  ${CMAKE_CURRENT_LIST_DIR}/px4-backend-stub.cpp
  ${CMAKE_CURRENT_LIST_DIR}/ardupilot-backend-stub.cpp
)

target_include_directories(omnisight-uav-autopilot-abstraction PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-uav-autopilot-abstraction PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-uav-autopilot-abstraction PRIVATE
  -Wall
  -Wextra
  -Werror
)

add_library(omnisight-uav-flight-mode-fsm STATIC
  ${CMAKE_CURRENT_LIST_DIR}/flight-mode-fsm.cpp
)

target_include_directories(omnisight-uav-flight-mode-fsm PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(omnisight-uav-flight-mode-fsm PUBLIC
  omnisight-uav-autopilot-abstraction
)

target_compile_features(omnisight-uav-flight-mode-fsm PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-uav-flight-mode-fsm PRIVATE
  -Wall
  -Wextra
  -Werror
)
