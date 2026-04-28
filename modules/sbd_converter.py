"""
sbd_converter.py — Steps 2 & 3: Convert .sbd/.sts files to profiles (CSV + TXT).

Data transformation:  SBD_FILES/ → (temp) .gz → (temp) .bin → PROFILES/

Adapted from:
  GreenlandFloats/processing/teledyne_scripts_to_functions/sbd_to_gz.py
  GreenlandFloats/processing/teledyne_scripts_to_functions/unzip_raw_files.py
  GreenlandFloats/processing/teledyne_scripts_to_functions/bin_to_csv.py

Key changes vs. originals:
  - Works on *copies* of .sbd files in a temp directory — SBD_FILES/ is never touched.
  - All incomplete_file_check / incomplete_gz_file logic removed (per instructions).
  - All shutil.copy/os.remove calls that organized .sbd/.sts into profile subfolders removed.
  - .meta file interactive prompt removed (not needed for conversion itself).
  - Structured error logging via FloatLogger.
  - .gz files deleted after decompression attempt; .bin files deleted after CSV conversion.
  - Resulting profiles moved to PROFILES/ directory.

Original features preserved:
  - Full SBD binary header parsing (START 0x01 / CONTINUATION 0x02 byte logic).
  - gzip error handling for incomplete .gz files (logs error, marks profile as failed, continues).
  - Full binary decoder tables from Teledyne apf11dec.py (science, vitals, ema, irad).
  - .bin cleanup after CSV conversion.
"""

import builtins
import glob
import gzip
import io
import json
import os
import shutil
import struct
import time
from typing import Optional, TypedDict
import zlib

from modules.logger import FloatLogger



class SbdConverterResult(TypedDict):
    converted_profiles: list[str]
    failed_profiles: list[str]
    new_profile_files: list[str]


# ============================================================================
# Binary decoder (from teledyne apf11dec.py / bin_to_csv.py)
# ============================================================================

WITH_ID = True
WITHOUT_ID = False


def _get_byte(byte: object) -> int:
    try:
        return ord(byte)
    except TypeError:
        return byte


def _unpack_with_final_asciiz(fmt, dat):
    if fmt[-1] != 'z':
        return struct.unpack(fmt, dat)
    str_len = len(dat) - struct.calcsize(fmt[:-1])
    new_fmt = '%s%ds' % (fmt[:-1], str_len)
    record = struct.unpack(new_fmt, dat)
    return (record[0], record[1].decode())


def _ts_str(ts: float) -> str:
    return time.strftime('%Y%m%dT%H%M%S', time.gmtime(ts))


def _f0_str(val: float) -> str: return '%0.0f' % val
def _f1_str(val: float) -> str: return '%0.1f' % val
def _f2_str(val: float) -> str: return '%0.2f' % val
def _f3_str(val: float) -> str: return '%0.3f' % val
def _f4_str(val: float) -> str: return '%0.4f' % val
def _f5_str(val: float) -> str: return '%0.5f' % val
def _f6_str(val: float) -> str: return '%0.6f' % val


class _datarecord:
    formats = {'T': 'I', '0': 'f', '1': 'f', '2': 'f', '3': 'f', '4': 'f', '5': 'f', '6': 'f'}
    posts = {'T': _ts_str, '0': _f0_str, '1': _f1_str, '2': _f2_str, '3': _f3_str,
             '4': _f4_str, '5': _f5_str, '6': _f6_str, 'f': _f5_str}

    def __init__(self, id, name, fmt, fields):
        self.id = id
        self.fmt = '<' + ''.join([_datarecord.formats.get(c) or c for c in fmt])
        self.post = tuple([_datarecord.posts.get(c) for c in fmt])
        self.name = name
        self.fields = fields

    def decode(self, record):
        if self.id is not None:
            id_ = struct.unpack('<BB', record[0:2])[0]
            if self.id != id_:
                return None
            data = _unpack_with_final_asciiz(self.fmt, record[1:])
        else:
            data = _unpack_with_final_asciiz(self.fmt, record)
        result = []
        for n, value in enumerate(data):
            result.append(self.post[n](value) if self.post[n] else value)
        return result


class _decoder:
    def __init__(self, with_id):
        self.records = {}
        self.with_id = with_id

    def __getitem__(self, id):
        return self.records[id].name

    def add_record_with_id(self, id, name, fmt, fields):
        self.records[id] = _datarecord(id, name, fmt, fields)

    def add_record_without_id(self, fmt, fields):
        self.records = _datarecord(None, None, fmt, fields)

    def fields(self):
        return self.records.fields

    def decode(self, data):
        if self.with_id:
            record_index = _get_byte(data[0])
            record = self.records[record_index]
            return [record.name] + record.decode(data)
        else:
            return self.records.decode(data)


# --- Vitals decoder ---
vitals = _decoder(WITH_ID)
vitals.add_record_with_id(0, 'Message', 'Tz', ('timestamp', 'message'))
vitals.add_record_with_id(1, 'VITALS_CORE', 'T3H3H333H33h',
    ('timestamp', 'air_bladder(dbar)', 'air_bladder(cnts)', 'battery_voltage(V)',
     'battery_voltage(cnts)', 'humidity', 'leak_detect(V)', 'vacuum(dbar)',
     'vacuum(cnts)', 'coulomb(AHrs)', 'battery_current(mA)', 'battery_current_raw'))
vitals.add_record_with_id(2, 'RSSI', 'TB', ('timestamp', 'RSSI'))
vitals.add_record_with_id(3, 'WD_CNT', 'Ti', ('Timestamp', 'Events(count)'))
vitals.add_record_with_id(25, 'AIR', 'TIIfHfffffffHHfHfHBfHffHHf',
    ('Timestamp', 'num_updates', 'total_seconds', 'last_battery_voltage_reading_in_volts',
     'last_battery_voltage_reading_in_counts', 'last_battery_current_reading_in_milliamps',
     'last_coulomb_reading_in_mA_hr', 'total_coulomb_usage_in_mA_hr',
     'battery_current_avg_in_milliamps', 'battery_current_max_in_milliamps',
     'battery_voltage_avg_in_volts', 'battery_voltage_min_in_volts',
     'battery_voltage_avg_in_counts', 'battery_voltage_min_in_counts',
     'internal_vacuum_pressure_in_dbar', 'internal_vacuum_pressure_counts',
     'air_bladder_pressure_in_dbar', 'air_bladder_pressure_counts', 'num_pulses',
     'last_air_pump_current_reading_in_milliamps', 'last_air_pump_current_reading_in_counts',
     'air_pump_current_avg_in_milliamps', 'air_pump_current_max_in_milliamps',
     'air_pump_current_avg_in_counts', 'air_pump_current_max_in_counts', 'air_pump_volt_secs'))
vitals.add_record_with_id(26, 'BUOYANCY', 'TIIfHfffffffHHHHfHffHHIIff',
    ('Timestamp', 'num_updates', 'total_seconds', 'last_battery_voltage_reading_in_volts',
     'last_battery_voltage_reading_in_counts', 'last_battery_current_reading_in_milliamps',
     'last_coulomb_reading_in_mA_hr', 'total_coulomb_usage_in_mA_hr',
     'battery_current_avg_in_milliamps', 'battery_current_max_in_milliamps',
     'battery_voltage_avg_in_volts', 'battery_voltage_min_in_volts',
     'battery_voltage_avg_in_counts', 'battery_voltage_min_in_counts',
     'start_position_count', 'stop_position_count',
     'last_buoy_pump_current_reading_in_milliamps', 'last_buoy_pump_current_reading_in_counts',
     'buoy_pump_current_avg_in_milliamps', 'buoy_pump_current_max_in_milliamps',
     'buoy_pump_current_avg_in_counts', 'buoy_pump_current_max_in_counts',
     'last_pot_change', 'total_pot_change', 'start_coulomb_in_amphrs', 'end_coulomb_in_amphrs'))
vitals.add_record_with_id(27, 'PRESSURE', 'TIIfHfffffffHHfHffH',
    ('Timestamp', 'num_updates', 'total_seconds', 'last_battery_voltage_reading_in_volts',
     'last_battery_voltage_reading_in_counts', 'last_battery_current_reading_in_milliamps',
     'last_coulomb_reading_in_mA_hr', 'total_coulomb_usage_in_mA_hr',
     'battery_current_avg_in_milliamps', 'battery_current_max_in_milliamps',
     'battery_voltage_avg_in_volts', 'battery_voltage_min_in_volts',
     'battery_voltage_avg_in_counts', 'battery_voltage_min_in_counts',
     'battery_voltage_in_volts', 'battery_voltage_in_counts', 'battery_current_in_milliamps',
     'current_in_milliamps', 'current_in_counts'))
vitals.add_record_with_id(28, 'COMMS', 'TIIfHfffffffHHI',
    ('Timestamp', 'num_updates', 'total_seconds', 'last_battery_voltage_reading_in_volts',
     'last_battery_voltage_reading_in_counts', 'last_battery_current_reading_in_milliamps',
     'last_coulomb_reading_in_mA_hr', 'total_coulomb_usage_in_mA_hr',
     'battery_current_avg_in_milliamps', 'battery_current_max_in_milliamps',
     'battery_voltage_avg_in_volts', 'battery_voltage_min_in_volts',
     'battery_voltage_avg_in_counts', 'battery_voltage_min_in_counts', 'total_num_bytes'))
vitals.add_record_with_id(50, 'ICE_DETECT', 'Ti34i', ('timestamp', 'mission', 'medianP', 'medianT', 'samples'))
vitals.add_record_with_id(51, 'ICE_CAP', 'Ti34i', ('timestamp', 'mission', 'medianP', 'medianT', 'samples'))
vitals.add_record_with_id(52, 'ICE_BREAKUP', 'Ti34i', ('timestamp', 'mission', 'medianP', 'medianT', 'samples'))

# --- Science decoder ---
science = _decoder(WITH_ID)
science.add_record_with_id(0, 'Message', 'Tz', ('timestamp', 'message'))
science.add_record_with_id(1, 'GPS', 'T66i', ('timestamp', 'latitude', 'longitude', 'nsat'))
science.add_record_with_id(10, 'CTD_bins', 'TIH3', ('timestamp', 'samples', 'bins', 'maxpress'))
science.add_record_with_id(11, 'CTD_P', 'T2', ('timestamp', 'pressure'))
science.add_record_with_id(12, 'CTD_PT', 'T24', ('timestamp', 'pressure', 'temperature'))
science.add_record_with_id(13, 'CTD_PTS', 'T244', ('timestamp', 'pressure', 'temperature', 'salinity'))
science.add_record_with_id(14, 'CTD_CP', 'T244h', ('timestamp', 'pressure', 'temperature', 'salinity', 'samples'))
science.add_record_with_id(15, 'CTD_PTSH', 'T2446', ('timestamp', 'pressure', 'temperature', 'salinity', 'ph'))
science.add_record_with_id(16, 'CTD_CP_H', 'T244h6h', ('timestamp', 'pressure', 'temperature', 'salinity', 'samples', 'ph', 'samples'))
science.add_record_with_id(20, 'LGR_PTSCI', 'Tfffff', ('timestamp', 'pressure', 'temperature', 'salinity', 'conductivity', 'internal_temperature'))
science.add_record_with_id(21, 'LGR_CP_PTSCI', 'Tfffffh', ('timestamp', 'pressure', 'temperature', 'salinity', 'conductivity', 'internal_temperature', 'samples'))
science.add_record_with_id(22, 'LGR_CP_PT', 'Tffh', ('timestamp', 'pressure', 'temperature', 'samples'))
science.add_record_with_id(23, 'LGR_P', 'Tf', ('timestamp', 'pressure'))
science.add_record_with_id(24, 'LGR_PT', 'Tff', ('timestamp', 'pressure', 'temperature'))
science.add_record_with_id(25, 'LGR_PTS', 'Tfff', ('timestamp', 'pressure', 'temperature', 'salinity'))
science.add_record_with_id(26, 'LGR_PTSC', 'Tffff', ('timestamp', 'pressure', 'temperature', 'salinity', 'conductivity'))
science.add_record_with_id(40, 'O2', 'Tffffffffff', ('timestamp', 'O2', 'AirSat', 'Temp', 'CalPhase', 'TCPhase', 'C1RPh', 'C2RPh', 'C1Amp', 'C2Amp', 'RawTemp'))
science.add_record_with_id(50, 'FLBB', 'Thhh', ('timestamp', 'chl_sig', 'bsc_sig', 'therm_sig'))
science.add_record_with_id(51, 'FLBB_BB', 'Thhhh', ('timestamp', 'chl_sig', 'bsc_sig0', 'bsc_sig1', 'therm_sig'))
science.add_record_with_id(52, 'FLBB_CD', 'Thhhh', ('timestamp', 'chl_sig', 'bcs_sig', 'cd_sig', 'therm_sig'))
science.add_record_with_id(53, 'FLBB_CFG', 'Thh', ('timestamp', 'chl_wave', 'bsc_wave'))
science.add_record_with_id(54, 'FLBB_BB_CFG', 'Thhh', ('timestamp', 'chl_wave', 'bsc_wave0', 'bsc_wave1'))
science.add_record_with_id(55, 'FLBB_CD_CFG', 'Thhh', ('timestamp', 'chl_wave', 'bsc_wave', 'cd_wave'))
science.add_record_with_id(60, '504R', 'Tffff', ('timestamp', 'channel1', 'channel2', 'channel3', 'channel4'))
science.add_record_with_id(61, '504I', 'Tffff', ('timestamp', 'channel1', 'channel2', 'channel3', 'channel4'))
science.add_record_with_id(70, 'CROVER', 'Thhhhf', ('timestamp', 'reference', 'raw_sig', 'corr_sig', 'therm', 'attenuation(m^-1)'))
science.add_record_with_id(80, 'Compass', 'Tffff', ('timestamp', 'heading', 'pitch', 'roll', 'dip'))
science.add_record_with_id(90, 'O2', 'THHHHHHI', ('timestamp', 'temperature', 'dissolved_oxygen', 'blue_phase', 'red_phase', 'blue_amplitude', 'red_amplitude', 'accumulated_led_time'))
science.add_record_with_id(100, 'NO3', 'T2', ('timestamp', 'nitrate'))
science.add_record_with_id(110, 'SRTC', 'TI', ('timestamp', 'rtc_time'))
science.add_record_with_id(115, 'RAFOS', 'TBHBHBHBHBHBH', ('timestamp', 'correlation1', 'sample1', 'correlation2', 'sample2', 'correlation3', 'sample3', 'correlation4', 'sample4', 'correlation5', 'sample5', 'correlation6', 'sample6'))
science.add_record_with_id(120, 'EMA', 'Tffffffffffffffffffffff', ('timestamp', 'rotp_hx', 'rotp_hy', 'e1_coef40', 'e1_coef41', 'e2_coef40', 'e2_coef41', 'e1_mean4', 'e2_mean4', 'e1_sdev4', 'e2_sdev4', 'buoy_pos_c0', 'hxdt_sdev', 'hydt_sdev', 'bt_mean', 'hz_mean', 'ax_mean', 'ax_sdev', 'ay_mean', 'ay_sdev', 'az_mean', 'hx_mean', 'hy_mean'))
science.add_record_with_id(121, 'EMA_CFG', 'TBBfB', ('timestamp', 'ReqSamples', 'SlideSamples', 'EMALogMaxPressure', 'EMALogMissionCycle'))
science.add_record_with_id(125, 'IRAD', 'THffff', ('timestamp', 'integration_time', 'temperature', 'pressure', 'pre_inclination', 'post_inclination'))

# --- EMA decoder ---
ema = _decoder(WITHOUT_ID)
ema.add_record_without_id('THBHHBBBBBBBBffffffffHHHHHHHHBdlBdlH',
    ('timestamp', 'buoy_pos', 'age', 'overflow', 'seqno',
     'ZR_nsum', 'BT_nsum', 'Hz_nsum', 'Hy_nsum', 'Hx_nsum', 'Az_nsum', 'Ay_nsum', 'Ax_nsum',
     'ZR_mean', 'BT_mean', 'Hz_mean', 'Hy_mean', 'Hx_mean', 'Az_mean', 'Ay_mean', 'Ax_mean',
     'ZR_pp', 'BT_pp', 'Hz_pp', 'Hy_pp', 'Hx_pp', 'Az_pp', 'Ay_pp', 'Ax_pp',
     'E1_nsum', 'E1_mean', 'E1_pp', 'E2_nsum', 'E2_mean', 'E2_pp', 'CRC'))

# --- IRAD decoder ---
irad = _decoder(WITHOUT_ID)
irad_rec_len = 514
irad.add_record_without_id(
    'T' + 'H' * 255,
    ['timestamp'])

EXPERIMENTAL = range(225, 255)
EXCLUDE_EXPERIMENTAL = False

SBD_NEW_FILE_START_CHAR = b'\x01'.decode('utf-8')
SBD_CONT_FILE_START_CHAR = b'\x02'.decode('utf-8')


# ============================================================================
# Internal pipeline steps
# ============================================================================

def _momsn_from_sbd_path(path: str) -> Optional[int]:
    """Extract the MOMSN sequence number from an SBD filename, or None if not parseable."""
    basename = os.path.basename(path)
    fnseq = basename[basename.rfind('_') + 1: basename.rfind('.')]
    try:
        return int(fnseq)
    except ValueError:
        return None


def _sbd_to_gz(sbd_src_dir: str, gz_out_dir: str, logger: FloatLogger) -> tuple[dict, list]:
    """
    Convert .sbd files from sbd_src_dir into .gz files in gz_out_dir.

    Adapted from sbd_to_gz.py::convert_sbd_to_gz().
    Changes: removed incomplete_file_check, removed file-moving into profile subfolders,
             removed .meta file check, removed incomplete_gz_files CSV tracking.

    Returns:
        profile_status:  dict mapping output_filename_path[:12] prefix → 'complete' | 'incomplete'
        profile_records: list of per-gz-file dicts with prof_num, MOMSN range, and message counts
    """
    num_msgs_to_process = 0
    output_filename_path = None
    msgfns = []
    prevmsg = b''
    output_file = None
    profile_status = {}

    # Per-gz-file state tracking
    profile_records: list = []
    _prof_num: Optional[str] = None
    _prof_start_momsn: Optional[int] = None
    _prof_last_momsn: Optional[int] = None
    _prof_total_msgs: int = 0
    _prof_msgs_received: int = 0

    sbd_files = sorted(glob.glob(os.path.join(sbd_src_dir, '*.sbd')))
    logger.info("PROFILE_CONVERSION", f"SBD files to process: {len(sbd_files)}")

    for current_sbd_path in sbd_files:
        sbd_data = io.open(current_sbd_path, 'rb').read()
        first_char = sbd_data[0]
        if isinstance(first_char, int):
            first_char = builtins.chr(first_char)

        momsn = _momsn_from_sbd_path(current_sbd_path)
        if momsn is None:
            logger.warning("PROFILE_CONVERSION", f"Invalid SBD filename: {os.path.basename(current_sbd_path)}")
            continue

        if first_char == SBD_NEW_FILE_START_CHAR:
            # Record previous incomplete gz file before starting a new one
            if output_filename_path and num_msgs_to_process != 0:
                logger.warning(
                    "PROFILE_CONVERSION",
                    f"Incomplete .gz: {output_filename_path} (missing {num_msgs_to_process} message(s))."
                )
                if _prof_num is not None:
                    profile_records.append({
                        "prof_num": _prof_num,
                        "momsn_range": [_prof_start_momsn, _prof_last_momsn],
                        "num_messages": _prof_total_msgs,
                        "num_messages_received": _prof_msgs_received,
                        "status": "incomplete",
                    })

            # Parse new file header
            nullpos = sbd_data.find(b'\0', 3)
            output_filename_path = sbd_data[3:nullpos].decode('utf-8')
            num_msgs_to_process = struct.unpack('>H', sbd_data[1:3])[0]

            if num_msgs_to_process <= 0:
                logger.warning("PROFILE_CONVERSION", f"Bad message count in SBD header: {os.path.basename(current_sbd_path)}")
                continue

            # Initialize tracking for this gz file
            _prof_num = output_filename_path[9:12] if len(output_filename_path) >= 12 else output_filename_path
            _prof_start_momsn = momsn
            _prof_last_momsn = momsn
            _prof_total_msgs = num_msgs_to_process  # total declared in header, before any decrement
            _prof_msgs_received = 1

            output_file = open(os.path.join(gz_out_dir, output_filename_path), 'wb')
            output_file.write(sbd_data[nullpos + 1:])
            msgfns = [current_sbd_path]
            num_msgs_to_process -= 1

        else:
            # Continuation message
            if first_char != SBD_CONT_FILE_START_CHAR:
                logger.warning("PROFILE_CONVERSION", f"Unknown SBD message type in {os.path.basename(current_sbd_path)}")
                continue
            if sbd_data == prevmsg:
                continue  # Retry duplicate — skip
            elif num_msgs_to_process <= 0:
                logger.warning("PROFILE_CONVERSION", f"Unexpected continuation message: {os.path.basename(current_sbd_path)}")
                continue
            else:
                prevmsg = sbd_data
                if output_file is not None:
                    output_file.write(sbd_data[1:])
                    msgfns.append(current_sbd_path)
                    num_msgs_to_process -= 1
                    _prof_last_momsn = momsn
                    _prof_msgs_received += 1

        # Check if .gz file is complete
        if num_msgs_to_process <= 0 and output_file is not None:
            output_file.close()
            output_file = None
            profile_status[output_filename_path[:12]] = 'complete'
            logger.info("PROFILE_CONVERSION", f"Created .gz: {output_filename_path}")
            if _prof_num is not None:
                profile_records.append({
                    "prof_num": _prof_num,
                    "momsn_range": [_prof_start_momsn, _prof_last_momsn],
                    "num_messages": _prof_total_msgs,
                    "num_messages_received": _prof_msgs_received,
                    "status": "complete",
                })
                _prof_num = None
            msgfns = []

    # Handle last incomplete file
    if output_filename_path and num_msgs_to_process != 0:
        logger.warning(
            "PROFILE_CONVERSION",
            f"Last .gz incomplete: {output_filename_path} (missing {num_msgs_to_process} message(s))."
        )
        profile_status[output_filename_path[:12]] = 'incomplete'
        if _prof_num is not None:
            profile_records.append({
                "prof_num": _prof_num,
                "momsn_range": [_prof_start_momsn, _prof_last_momsn],
                "num_messages": _prof_total_msgs,
                "num_messages_received": _prof_msgs_received,
                "status": "incomplete",
            })
        if output_file:
            output_file.close()

    return profile_status, profile_records


def _unzip_gz(work_dir: str, profile_status: dict, logger: FloatLogger) -> set:
    """
    Decompress complete .gz files in work_dir to .bin files.
    Deletes .gz files after each attempt (success or failure).
    Returns set of profile prefixes that failed decompression.
    """
    failed_profiles = set()

    for file_name in os.listdir(work_dir):
        if not (file_name.endswith('bin.gz') or file_name.endswith('txt.gz')):
            continue
        if file_name.startswith('.'):
            continue

        prefix = file_name[:12]
        src_gz = os.path.join(work_dir, file_name)
        dst_bin = os.path.join(work_dir, file_name[:-3])  # strip .gz

        try:
            with gzip.open(src_gz, 'rb') as f_in:
                with open(dst_bin, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
        except EOFError as e:
            logger.error(
                "PROFILE_CONVERSION",
                f"Failed to decompress {file_name}: {e} "
                f"ERROR : {e}"
            )
            failed_profiles.add(prefix)
            if os.path.exists(dst_bin):
                os.remove(dst_bin)
        except zlib.error as e:
            logger.error(
                "PROFILE_CONVERSION",
                f"Failed to decompress {file_name}: {e} "
                f"ERROR : {e}"
            )
            failed_profiles.add(prefix)
            if os.path.exists(dst_bin):
                os.remove(dst_bin)

        # Always delete .gz after attempt
        try:
            os.remove(src_gz)
        except OSError:
            pass

    return failed_profiles


def _bin_to_csv(work_dir: str, logger: FloatLogger) -> list:
    """
    Decode all .bin files in work_dir to .csv (and preserve .txt files).
    Deletes .bin files after conversion.
    Returns list of output profile file basenames.
    """
    bin_files = sorted(glob.glob(os.path.join(work_dir, '*.bin')))
    output_files = []
    logger.info("PROFILE_CONVERSION", f"BIN files to decode: {len(bin_files)}")

    for bin_path in bin_files:
        csv_path = os.path.splitext(bin_path)[0] + '.csv'
        try:
            with open(bin_path, 'rb') as ifile, open(csv_path, 'w') as ofile:
                # Write EMA header (no ID field, so fields need explicit header)
                if 'ema_log' in bin_path:
                    ofile.write(','.join([str(x) for x in ema.fields()]) + '\n')

                while True:
                    if 'irad_log' in bin_path:
                        rec_len = irad_rec_len
                        rec = ifile.read(rec_len)
                        if len(rec) == 0:
                            break
                    else:
                        len_chr = ifile.read(1)
                        if len(len_chr) < 1:
                            break
                        rec_len = ord(len_chr)
                        rec = ifile.read(rec_len)
                        record_id = _get_byte(rec[0])
                        if record_id in EXPERIMENTAL and EXCLUDE_EXPERIMENTAL:
                            continue

                    try:
                        if 'science_log' in bin_path:
                            ofile.write(','.join([str(x) for x in science.decode(rec)]) + '\n')
                        elif 'vitals_log' in bin_path:
                            ofile.write(','.join([str(x) for x in vitals.decode(rec)]) + '\n')
                        elif 'ema_log' in bin_path:
                            ofile.write(','.join([str(x) for x in ema.decode(rec)]) + '\n')
                        elif 'irad_log' in bin_path:
                            ofile.write(','.join([str(x) for x in irad.decode(rec)]) + '\n')
                    except Exception:
                        pass  # Bad record — skip, continue

            output_files.append(os.path.basename(csv_path))
            logger.info("PROFILE_CONVERSION", f"Decoded: {os.path.basename(csv_path)}")

        except Exception as e:
            logger.error("PROFILE_CONVERSION", f"Failed to decode {os.path.basename(bin_path)}: {e}")
        finally:
            # Always delete .bin file
            try:
                os.remove(bin_path)
            except OSError:
                pass

    return output_files


def _aggregate_profile_records(records: list) -> list:
    """
    Collapse per-gz-file records into one entry per prof_num.
    - momsn_range: [min start, max end] across all gz files for that profile
    - num_messages / num_messages_received: summed
    - status: 'incomplete' if any gz file was incomplete, else 'complete'
    """
    groups: dict[str, dict] = {}
    for r in records:
        key = r["prof_num"]
        if key not in groups:
            groups[key] = {
                "prof_num": key,
                "momsn_range": list(r["momsn_range"]),
                "num_messages": r["num_messages"],
                "num_messages_received": r["num_messages_received"],
                "status": r["status"],
            }
        else:
            g = groups[key]
            g["momsn_range"][0] = min(g["momsn_range"][0], r["momsn_range"][0])
            g["momsn_range"][1] = max(g["momsn_range"][1], r["momsn_range"][1])
            g["num_messages"] += r["num_messages"]
            g["num_messages_received"] += r["num_messages_received"]
            if r["status"] == "incomplete":
                g["status"] = "incomplete"
    return list(groups.values())


def _compute_start_momsn(profiles: list, all_sbd_files: list, new_sbd_basenames: set) -> Optional[int]:
    """
    Determine the earliest MOMSN that needs reprocessing.

    Returns the minimum MOMSN to process from, or None if all files should be processed.

    Logic:
      1. Find the min MOMSN among the new (previously unseen) SBD files.
      2. Find the largest prof_num that is complete.
      3. If there are incomplete profiles after that last-complete prof_num,
         pull their start MOMSNs in too — those SBDs must be reprocessed alongside the new ones.
    """
    new_momsns = [
        m for f in all_sbd_files
        if os.path.basename(f) in new_sbd_basenames
        and (m := _momsn_from_sbd_path(f)) is not None
    ]
    if not new_momsns:
        return None
    min_new_momsn = min(new_momsns)

    if not profiles:
        return min_new_momsn

    complete_nums = [p["prof_num"] for p in profiles if p["status"] == "complete"]
    last_complete = max(complete_nums) if complete_nums else None

    if last_complete is not None:
        incomplete_after = [
            p for p in profiles
            if p["status"] == "incomplete" and p["prof_num"] > last_complete
        ]
    else:
        incomplete_after = [p for p in profiles if p["status"] == "incomplete"]

    if incomplete_after:
        min_incomplete_momsn = min(p["momsn_range"][0] for p in incomplete_after)
        return min(min_incomplete_momsn, min_new_momsn)

    return min_new_momsn


# ============================================================================
# Public API
# ============================================================================

def run(
    sbd_dir: str,
    profiles_dir: str,
    float_id: str,
    logger: FloatLogger,
    force_reprocess: bool = False,
) -> SbdConverterResult:
    """
    Convert SBD files to profiles (CSV + TXT).

    Reads from sbd_dir (SBD_FILES/ — originals untouched).
    Writes profiles to profiles_dir (PROFILES/).

    Args:
        sbd_dir:          Path to SBD_FILES/ directory (read-only source).
        profiles_dir:     Path to PROFILES/ output directory.
        float_id:         Float ID string (e.g. 'F9184'), used in logging.
        logger:           FloatLogger instance.
        force_reprocess:  If True, overwrite existing profile files in PROFILES/.

    Returns:
        dict with keys:
            converted_profiles (list of str — profile prefixes successfully converted)
            failed_profiles    (list of str — profile prefixes that failed)
            new_profile_files  (list of str — profile filenames moved to PROFILES/)
    """
    os.makedirs(profiles_dir, exist_ok=True)

    # --- All early-exit checks happen BEFORE tmp_dir is created ---

    # Check for SBD files
    sbd_files = sorted(glob.glob(os.path.join(sbd_dir, '*.sbd')))
    if not sbd_files:
        logger.info("PROFILE_CONVERSION", "No .sbd files found in SBD_FILES — nothing to convert.")
        logger.success("PROFILE_CONVERSION")
        return {"converted_profiles": [], "failed_profiles": [], "new_profile_files": []}

    _sbd_state_file = os.path.join(profiles_dir, ".sbd_conversion_state.json")
    current_sbd_names = [os.path.basename(f) for f in sbd_files]  # already sorted

    # Load last state if available
    _last_state: dict = {}
    if not force_reprocess and os.path.exists(_sbd_state_file):
        with open(_sbd_state_file) as _f:
            _last_state = json.load(_f)

    last_processed_set = set(_last_state.get("processed_sbd_files", []))
    new_sbd_basenames = set(current_sbd_names) - last_processed_set

    if not force_reprocess and not new_sbd_basenames:
        logger.info("PROFILE_CONVERSION", "No new SBD files since last conversion — skipping.")
        logger.success("PROFILE_CONVERSION")
        return {"converted_profiles": [], "failed_profiles": [], "new_profile_files": []}

    # Compute starting MOMSN for incremental processing
    profiles_state = _last_state.get("profiles", [])
    if force_reprocess or not profiles_state:
        start_momsn = None  # full reprocess
    else:
        start_momsn = _compute_start_momsn(profiles_state, sbd_files, new_sbd_basenames)

    # Filter to only the SBD files that need processing
    if start_momsn is not None:
        sbd_files_to_copy = [f for f in sbd_files if (_momsn_from_sbd_path(f) or -1) >= start_momsn]
        logger.info("PROFILE_CONVERSION", f"Incremental run: {len(sbd_files_to_copy)} SBD files from MOMSN {start_momsn}.")
    else:
        sbd_files_to_copy = sbd_files
        logger.info("PROFILE_CONVERSION", f"Full run: processing all {len(sbd_files_to_copy)} SBD files.")

    # Past all early exits — now create tmp_dir and run conversion
    tmp_dir = os.path.join(os.path.dirname(profiles_dir), "tmp_dir")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        # Step 1: Copy relevant .sbd files to temp dir (originals untouched)
        for f in sbd_files_to_copy:
            shutil.copy2(f, tmp_dir)
        logger.info("PROFILE_CONVERSION", f"Copied {len(sbd_files_to_copy)} .sbd files to temp dir.")

        # Step 2: SBD → .gz
        profile_status, profile_records = _sbd_to_gz(tmp_dir, tmp_dir, logger)

        # Step 3: .gz → .bin (delete .gz)
        failed_gz = _unzip_gz(tmp_dir, profile_status, logger)

        # Step 4: .bin → .csv (delete .bin)
        _bin_to_csv(tmp_dir, logger)

        # Step 5: Move profile files (.csv, .txt) to PROFILES/ dir
        new_profile_files = []
        for ext in ('*.csv', '*.txt'):
            for src in glob.glob(os.path.join(tmp_dir, ext)):
                src_basename = os.path.basename(src)
                dst = os.path.join(profiles_dir, src_basename)
                if os.path.exists(dst) and not force_reprocess:
                    logger.file_only("PROFILE_CONVERSION", f"Skipping existing: {src_basename}")
                    continue
                shutil.move(src, dst)
                new_profile_files.append(src_basename)

        logger.info("PROFILE_CONVERSION", f"Moved {len(new_profile_files)} profile files to PROFILES/.")

        # Merge old complete profiles (not reprocessed this run) with new records,
        # then save state so subsequent runs can detect new SBD files and skip unchanged profiles.
        old_profiles = _last_state.get("profiles", [])
        reprocessed_prof_nums = {r["prof_num"] for r in profile_records}
        preserved = [p for p in old_profiles if p["prof_num"] not in reprocessed_prof_nums]
        with open(_sbd_state_file, 'w') as _f:
            json.dump({
                "processed_sbd_files": current_sbd_names,
                "profiles": _aggregate_profile_records(preserved + profile_records),
            }, _f, indent=2)

        # Determine converted vs failed profile prefixes
        converted = [p for p, s in profile_status.items() if s == 'complete' and p not in failed_gz]
        failed = [p for p, s in profile_status.items() if s == 'incomplete'] + list(failed_gz)

        if not failed:
            logger.success("PROFILE_CONVERSION")

        return {
            "converted_profiles": converted,
            "failed_profiles": list(set(failed)),
            "new_profile_files": new_profile_files,
        }

    finally:
        # Clean up temp dir if it was created
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("PROFILE_CONVERSION", "Temp dir cleaned up.")
