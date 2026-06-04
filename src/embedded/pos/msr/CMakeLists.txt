# Case 7 POS MSR magstripe reader driver CMake fragment (OP-2055).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-pos-msr-magstripe-driver STATIC
  ${CMAKE_CURRENT_LIST_DIR}/msr-magstripe-driver.cpp
)
target_include_directories(omnisight-pos-msr-magstripe-driver PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-msr-magstripe-driver PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-msr-magstripe-driver PRIVATE -Wall -Wextra -Werror)

add_executable(omnisight-pos-msr-magstripe-driver-smoke
  ${CMAKE_CURRENT_LIST_DIR}/msr-magstripe-driver.cpp
)
target_compile_definitions(omnisight-pos-msr-magstripe-driver-smoke PRIVATE
  OMNISIGHT_POS_MSR_MAGSTRIPE_DRIVER_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-msr-magstripe-driver-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-pos-msr-magstripe-driver-smoke PRIVATE -Wall -Wextra -Werror)
