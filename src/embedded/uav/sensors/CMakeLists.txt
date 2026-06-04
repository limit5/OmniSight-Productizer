# Case 6 UAV sensor drivers CMake fragment (OP-2025 GPS, OP-2035 barometer).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

# ── GPS NMEA-0183 + UBX driver (OP-2025) ────────────────────────────

add_library(omnisight-uav-gps-nmea-driver STATIC
  ${CMAKE_CURRENT_LIST_DIR}/gps-nmea-driver.cpp
)
target_include_directories(omnisight-uav-gps-nmea-driver PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-uav-gps-nmea-driver PUBLIC cxx_std_17)
target_compile_options(omnisight-uav-gps-nmea-driver PRIVATE -Wall -Wextra -Werror)

add_executable(omnisight-uav-gps-nmea-driver-smoke
  ${CMAKE_CURRENT_LIST_DIR}/gps-nmea-driver.cpp
)
target_compile_definitions(omnisight-uav-gps-nmea-driver-smoke PRIVATE
  OMNISIGHT_UAV_GPS_NMEA_DRIVER_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-gps-nmea-driver-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-uav-gps-nmea-driver-smoke PRIVATE -Wall -Wextra -Werror)

# ── BMP388 / MS5611 barometer driver (OP-2035) ──────────────────────

add_library(omnisight-uav-barometer-driver STATIC
  ${CMAKE_CURRENT_LIST_DIR}/barometer-driver.cpp
)
target_include_directories(omnisight-uav-barometer-driver PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-uav-barometer-driver PUBLIC cxx_std_17)
target_compile_options(omnisight-uav-barometer-driver PRIVATE -Wall -Wextra -Werror)

add_executable(omnisight-uav-barometer-driver-smoke
  ${CMAKE_CURRENT_LIST_DIR}/barometer-driver.cpp
)
target_compile_definitions(omnisight-uav-barometer-driver-smoke PRIVATE
  OMNISIGHT_UAV_BAROMETER_DRIVER_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-barometer-driver-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-uav-barometer-driver-smoke PRIVATE -Wall -Wextra -Werror)
