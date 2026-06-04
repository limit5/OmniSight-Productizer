# Case 7 scanner + receipt printer unified integration CMake fragment (OP-2058).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-pos-scanner-printer-unified STATIC
  ${CMAKE_CURRENT_LIST_DIR}/scanner-printer-unified.cpp
)
target_include_directories(omnisight-pos-scanner-printer-unified PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/../scanner
)
target_compile_features(omnisight-pos-scanner-printer-unified PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-scanner-printer-unified PRIVATE
  -Wall
  -Wextra
  -Werror
)

add_executable(omnisight-pos-scanner-printer-unified-smoke
  ${CMAKE_CURRENT_LIST_DIR}/scanner-printer-unified.cpp
)
target_compile_definitions(omnisight-pos-scanner-printer-unified-smoke PRIVATE
  OMNISIGHT_POS_SCANNER_PRINTER_UNIFIED_SMOKE_MAIN=1
)
target_include_directories(omnisight-pos-scanner-printer-unified-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
  ${CMAKE_CURRENT_LIST_DIR}/../scanner
)
target_compile_features(omnisight-pos-scanner-printer-unified-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-scanner-printer-unified-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
