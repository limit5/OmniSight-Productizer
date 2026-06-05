# Cases 6+7 POS unified card-input router CMake fragment (OP-2063).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-pos-card-input-router STATIC
  ${CMAKE_CURRENT_LIST_DIR}/unified-card-input.cpp
)
target_include_directories(omnisight-pos-card-input-router PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/../emv
  ${CMAKE_CURRENT_LIST_DIR}/../msr
  ${CMAKE_CURRENT_LIST_DIR}/../nfc
)
target_compile_features(omnisight-pos-card-input-router PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-card-input-router PRIVATE
  -Wall
  -Wextra
  -Werror
)

add_executable(omnisight-pos-card-input-router-smoke
  ${CMAKE_CURRENT_LIST_DIR}/unified-card-input.cpp
)
target_include_directories(omnisight-pos-card-input-router-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/../emv
  ${CMAKE_CURRENT_LIST_DIR}/../msr
  ${CMAKE_CURRENT_LIST_DIR}/../nfc
)
target_compile_definitions(omnisight-pos-card-input-router-smoke PRIVATE
  OMNISIGHT_POS_CARD_INPUT_ROUTER_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-card-input-router-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-pos-card-input-router-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
