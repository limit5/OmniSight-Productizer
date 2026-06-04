# Case 7 TDES-DUKPT key derivation CMake fragment (OP-2030).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-pos-p2pe-tdes-dukpt STATIC
  ${CMAKE_CURRENT_LIST_DIR}/tdes-dukpt.cpp
)

target_include_directories(omnisight-pos-p2pe-tdes-dukpt PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-pos-p2pe-tdes-dukpt PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-p2pe-tdes-dukpt PRIVATE
  -Wall
  -Wextra
  -Werror
)

add_executable(omnisight-pos-p2pe-tdes-dukpt-smoke
  ${CMAKE_CURRENT_LIST_DIR}/tdes-dukpt.cpp
)
target_compile_definitions(omnisight-pos-p2pe-tdes-dukpt-smoke PRIVATE
  OMNISIGHT_TDES_DUKPT_SMOKE_MAIN=1
)
target_include_directories(omnisight-pos-p2pe-tdes-dukpt-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-p2pe-tdes-dukpt-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-p2pe-tdes-dukpt-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
