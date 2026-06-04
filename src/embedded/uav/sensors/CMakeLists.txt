# Case 6 UAV GPS NMEA-0183 + UBX driver CMake fragment (OP-2025).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-uav-gps-nmea-driver STATIC
  ${CMAKE_CURRENT_LIST_DIR}/gps-nmea-driver.cpp
)

target_include_directories(omnisight-uav-gps-nmea-driver PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-uav-gps-nmea-driver PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-uav-gps-nmea-driver PRIVATE
  -Wall
  -Wextra
  -Werror
)

include(CheckIncludeFileCXX)
check_include_file_cxx(termios.h OMNISIGHT_GPS_HAS_TERMIOS_HEADER)

if(OMNISIGHT_GPS_HAS_TERMIOS_HEADER)
  target_compile_definitions(omnisight-uav-gps-nmea-driver PRIVATE
    OMNISIGHT_GPS_WITH_POSIX_SERIAL=1
  )
endif()

add_executable(omnisight-uav-gps-nmea-driver-smoke
  ${CMAKE_CURRENT_LIST_DIR}/gps-nmea-driver.cpp
)
target_compile_definitions(omnisight-uav-gps-nmea-driver-smoke PRIVATE
  OMNISIGHT_GPS_NMEA_DRIVER_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-gps-nmea-driver-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-uav-gps-nmea-driver-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
