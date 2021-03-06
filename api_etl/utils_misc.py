"""
Module containing some useful functions that might be used by all other
modules.
"""

from os import sys, path, listdir, rmdir, remove, makedirs
from os.path import isfile, join
import logging
from logging.handlers import RotatingFileHandler
from dateutil.tz import tzlocal
import pytz
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import boto3

from api_etl.settings import (
    __DATA_PATH__, __RESPONDING_STATIONS_PATH__,
    __ALL_STATIONS_PATH__, __TOP_STATIONS_PATH__,
    __SCHEDULED_STATIONS_PATH__, __LOGS_PATH__,
    __STATIONS_PER_LINE_PATH__
)
from api_etl.utils_secrets import get_secret

logger = logging.getLogger(__name__)

AWS_DEFAULT_REGION = get_secret("AWS_DEFAULT_REGION", env=True)
AWS_ACCESS_KEY_ID = get_secret("AWS_ACCESS_KEY_ID", env=True)
AWS_SECRET_ACCESS_KEY = get_secret("AWS_SECRET_ACCESS_KEY", env=True)


def build_uri(
    db_type, host, user=None, password=None,
    port=None, database=None
):
    uri = "%s://" % db_type
    if user and password:
        uri += "%s:%s@" % (quote_plus(user), quote_plus(password))
    uri += host
    if port:
        uri += ":" + str(port)
    if database:
        uri += "/%s" % quote_plus(database)
    return uri


def chunks(l, n):
    """
    Yield a list in 'n' lists of nearly same size (some can be one more than
    others).

    :param l: list you want to divide in chunks
    :type l: list

    :param n: number of chunks you want to get
    :type n: int
    """
    for i in range(0, len(l), n):
        yield l[i:i + n]


class StationProvider:
    """ Class to easily get lists of stations in gtfs format (7 digits) or
    transilien's format (8 digits).

    Warning: data sources have to be checked ("all" is ok, "top" is wrong).
    """

    def __init__(self):
        self._all_stations_path = __ALL_STATIONS_PATH__
        self._responding_stations_path = __RESPONDING_STATIONS_PATH__
        self._top_stations_path = __TOP_STATIONS_PATH__
        self._scheduled_stations_path = __SCHEDULED_STATIONS_PATH__
        self._stations_per_line_path = __STATIONS_PER_LINE_PATH__

    def get_stations_per_line(self, lines=None, uic7=False, full_df=False):
        """
        Get stations of given line (multiple lines possible)
        :param lines:
        :param uic7:
        :param full_df:
        """
        if lines:
            assert isinstance(lines, list)

        lines = lines or ['C', 'D', 'E', 'H', 'J', 'K', 'N', 'P', 'U']
        # all but 'A', 'Aéroport C', 'B', 'T4', 'L', 'R'
        station_path = self._stations_per_line_path
        df = pd.read_csv(station_path, sep=";")
        matching_stop_times = df.dropna(axis=0, how="all", subset=lines)

        if full_df:
            return matching_stop_times

        stations = matching_stop_times.Code_UIC.apply(str).tolist()
        if not uic7:
            return stations

        return list(map(lambda x: x[0: -1], stations))

    def get_station_ids(self, stations="all", gtfs_format=False):
        """
        Get stations ids either in API format (8 digits), or in GTFS format
        (7 digits).

        Beware, this function has to be more tested.
        Beware: two formats:
        - 8 digits format to query api
        - 7 digits format to query gtfs files
        :param stations:
        :param gtfs_format:
        """
        if stations == "all":
            station_path = self._all_stations_path

        elif stations == "responding":
            station_path = self._responding_stations_path

        elif stations == "top":
            station_path = self._top_stations_path

        elif stations == "scheduled":
            station_path = self._scheduled_stations_path

        else:
            raise ValueError(
                "stations parameter should be either 'all', 'top'," +
                " 'scheduled' or 'responding'"
            )

        station_ids = np.genfromtxt(station_path, delimiter=",", dtype=str)

        if gtfs_format:
            # Remove last character
            station_ids = map(lambda x: x[:-1], station_ids)

        return list(station_ids)


class DateConverter:
    """Class to convert dates from and to our special format, from and to api
    date format, and to and from our regular format:
    \n- api_format: "16/02/2017 01:26"
    \n- normal date: "20170216"
    \n- normal time: "01:26:00"
    \n- special date: "20170215"
    \n- special time: "25:26:00"

    \nThis class has also methods to compute delays
    """

    def __init__(
        self, dt=None, api_date=None, normal_date=None, normal_time=None,
        special_date=None, special_time=None, force_regular_date=False
    ):
        """Works in two steps, first try to find real datetime from arguments
        passed, then computes string representations.
        """
        self.dt = dt
        self.api_date = api_date
        self.normal_date = normal_date
        self.normal_time = normal_time
        self.special_date = special_date
        self.special_time = special_time

        if self.api_date:
            self._api_date_to_dt()

        elif self.normal_date and self.normal_time:
            self._normal_datetime_to_dt()

        elif self.special_time and self.special_date:
            self._special_datetime_to_dt(force_regular_date)

        else:
            assert self.dt

        self.api_date = self.dt.strftime("%d/%m/%Y %H:%M")
        self.normal_date = self.dt.strftime("%Y%m%d")
        self.normal_time = self.dt.strftime("%H:%M:%S")
        # Compute special datetime self.special_date and self.special_time
        self._dt_to_special_datetime()

    def _api_date_to_dt(self):
        assert self.api_date
        self.dt = datetime.strptime(self.api_date, "%d/%m/%Y %H:%M")

    def _normal_datetime_to_dt(self):
        assert (self.normal_date and self.normal_time)
        # "2017021601:26:00"
        full_str_dt = "%s%s" % (self.normal_date, self.normal_time)
        self.dt = datetime.strptime(full_str_dt, "%Y%m%d%H:%M:%S")

    def _special_datetime_to_dt(self, force_regular_date):
        assert(self.special_date and self.special_time)
        hour = self.special_time[:2]
        assert (0 <= int(hour) < 29)
        add_day = False
        if int(hour) in (24, 25, 26, 27):
            hour = str(int(hour) - 24)
            add_day = True
        corr_sp_t = hour + self.special_time[2:]
        full_str_dt = "%s%s" % (self.special_date, corr_sp_t)
        dt = datetime.strptime(full_str_dt, "%Y%m%d%H:%M:%S")
        if add_day and not force_regular_date:
            dt = dt + timedelta(days=1)
        self.dt = dt

    def _dt_to_special_datetime(self):
        """
        Dates between 0 and 3 AM are transformed in +24h time format with
        day as previous day.
        """
        assert self.dt
        # For hours between 00:00:00 and 02:59:59: we add 24h and say it
        # is from the day before
        if self.dt.hour in (0, 1, 2):
            # say this train is departed the day before
            special_dt = self.dt - timedelta(days=1)
            self.special_date = special_dt.strftime("%Y%m%d")
            # +24: 01:44:00 -> 25:44:00
            self.special_time = "%s:%s" % (
                self.dt.hour + 24, self.dt.strftime("%M:%S"))
        else:
            self.special_date = self.dt.strftime("%Y%m%d")
            self.special_time = self.dt.strftime("%H:%M:%S")

    def compute_delay_from(
        self, dc=None, dt=None, api_date=None, normal_date=None,
        normal_time=None, special_date=None, special_time=None,
        force_regular_date=False
    ):
        """
        Create another DateConverter and compares datetimes
        Return in seconds the delay:
        - positive if this one > 'from' (delayed)
        - negative if this one < 'from' (advance)
        :param dc:
        :param dt:
        :param api_date:
        :param normal_date:
        :param normal_time:
        :param special_date:
        :param special_time:
        :param force_regular_date:
        """
        if dc:
            assert isinstance(dc, DateConverter)
            other_dt = dc.dt
        else:
            other_dt = DateConverter(
                api_date=api_date, normal_date=normal_date,
                normal_time=normal_time, dt=dt,
                special_date=special_date, special_time=special_time, force_regular_date=force_regular_date
            ).dt
        time_delta = self.dt - other_dt

        return time_delta.total_seconds()


def get_paris_local_datetime_now(tz_naive=True):
    """
    Return paris local time (necessary for operations operated on other time
    zones)
    :param tz_naive:
    """
    paris_tz = pytz.timezone('Europe/Paris')
    datetime_paris = datetime.now(tzlocal()).astimezone(paris_tz)
    if tz_naive:
        return datetime_paris.replace(tzinfo=None)
    else:
        return datetime_paris


def set_logging_conf(log_name, level="INFO"):

    # Python crashes or captured as well (beware of ipdb imports)
    def handle_exception(exc_type, exc_value, exc_traceback):
        # if issubclass(exc_type, KeyboardInterrupt):
        #    sys.__excepthook__(exc_type, exc_value, exc_traceback)
        logger.error("Uncaught exception : ", exc_info=(
            exc_type, exc_value, exc_traceback))
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = handle_exception


def get_responding_stations_from_sample(sample_loc=None, write_loc=None):
    """
    This function's purpose is to write down responding stations from a given
    "real_departures" sample, and to write it down so it can be used to query
    only necessary stations (and avoid to spend API credits on unnecessary
    stations)
    :param sample_loc:
    :param write_loc:
    """
    if not sample_loc:
        sample_loc = path.join(__DATA_PATH__, "20170131_real_departures.csv")
    if not write_loc:
        write_loc = __RESPONDING_STATIONS_PATH__

    df = pd.read_csv(sample_loc)
    resp_stations = df["station"].unique()
    np.savetxt(write_loc, resp_stations, delimiter=",", fmt="%s")

    return list(resp_stations)

# S3 functions


def s3_ressource():
    # Credentials are accessed via environment variables
    s3 = boto3.resource('s3', region_name=AWS_DEFAULT_REGION)
    return s3


class S3Bucket:

    def __init__(self, name, create_if_absent=False):
        self._s3 = s3_ressource()
        self.bucket_name = name
        self._selected_bucket_objects_keys = []
        self._check_if_accessible()
        if create_if_absent and not self._accessible:
            self._create_bucket()

    def _check_if_accessible(self):
        try:
            self._s3.meta.client.head_bucket(Bucket=self.bucket_name)
            self._accessible = True
            self.bucket = self._s3.Bucket(self.bucket_name)
            logger.info("Bucket %s is accessible." % self.bucket_name)
            return True

        except Exception as e:
            # If a client error is thrown, then check that it was a 404 error.
            # If it was a 404 error, then the bucket does not exist.
            self._accessible = False
            logger.info("Bucket %s does not exist." % self.bucket_name)
            logger.debug("Could not access bucket %s: %s" %
                          (self.bucket_name, e))
            return False

    def _create_bucket(self):
        assert not self._accessible
        logger.info("Creating bucket %s" % self.bucket_name)
        self._s3.create_bucket(
            Bucket=self.bucket_name,
            CreateBucketConfiguration={
                'LocationConstraint': AWS_DEFAULT_REGION}
        )
        self._check_if_accessible()

    def send_file(self, file_local_path, file_remote_path=None, delete=False, ignore_hidden=False):
        if not file_remote_path:
            file_remote_path = path.relpath(file_local_path, start=__DATA_PATH__)

        if ignore_hidden:
            # get file name (without path)
            file_name = path.basename(path.normpath(file_local_path))
            if file_name.startswith("."):
                return None

        logger.info("Saving file '%s', as '%s' in bucket '%s'." %
                    (file_local_path, file_remote_path, self.bucket_name))
        self._s3.Object(self.bucket_name, file_remote_path)\
            .put(Body=open(file_local_path, 'rb'))

        if delete:
            remove(file_local_path)

    def send_folder(self, folder_local_path, folder_remote_path=None, delete=False, ignore_hidden=True):
        """Will keep same names for files inside folder.

        Note: in S3, there is no folder, just files with names as path.
        :param folder_local_path:
        :param folder_remote_path:
        :param delete:
        :param ignore_hidden:
        """
        # if no new name specified, use existing name
        if not folder_remote_path:
            folder_remote_path = path.relpath(folder_local_path, start=__DATA_PATH__)

        if ignore_hidden:
            folder_name = path.basename(path.normpath(folder_local_path))
            if folder_name.startswith("."):
                return None

        logger.info("Saving folder '%s', as '%s' in bucket '%s'." %
                    (folder_local_path, folder_remote_path, self.bucket_name))

        files = [f for f in listdir(
            folder_local_path) if isfile(join(folder_local_path, f))]
        subfolders = [f for f in listdir(
            folder_local_path) if not isfile(join(folder_local_path, f))]

        # new file names:

        for f in files:
            self.send_file(
                file_local_path=join(folder_local_path, f),
                file_remote_path=join(folder_remote_path, f),
                delete=delete
            )

        for subf in subfolders:
            self.send_folder(
                folder_local_path=join(folder_local_path, subf),
                folder_remote_path=join(folder_remote_path, subf),
                delete=delete
            )

        if delete:
            rmdir(folder_local_path)

    def list_bucket_objects(self, prefix=None):
        self._selected_bucket_objects_keys = []

        if not prefix:
            objects_summaries = self.bucket.objects.all()
        else:
            objects_summaries = self.bucket.objects.filter(Prefix=prefix)

        for obj in objects_summaries:
            self._selected_bucket_objects_keys.append(obj.key)

        return self._selected_bucket_objects_keys

    def download_file(self, file_remote_key, file_local_path=None, ignore_hidden=False):
        file_local_path = file_local_path or path.join(__DATA_PATH__, file_remote_key)

        if ignore_hidden:
            file_name = path.basename(path.normpath(file_remote_key))
            if file_name.startswith("."):
                return None
        logger.info("Download of '%s' as '%s'." % (file_remote_key, file_local_path))
        self.bucket.download_file(Key=file_remote_key, Filename=file_local_path)


    def download_folder(self, remote_prefix=None, local_folder_root=None, ignore_hidden=True):
        """
        Everything is saved in data folder, according to key hierarchy.

        :param remote_prefix:
        :param ignore_hidden:
        :return:
        """

        if ignore_hidden:
            folder_name = path.basename(path.normpath(remote_prefix))
            if folder_name.startswith("."):
                return None

        local_folder_root = local_folder_root or path.join(__DATA_PATH__, "downloads")

        # check all files in <remote_prefix> (key beginning with <remote_prefix>)
        # if None, takes everything in bucket
        keys_to_download = self.list_bucket_objects(prefix=remote_prefix)

        # download
        for key in keys_to_download:
            file_local_path = path.join(local_folder_root, key)
            file_dir = path.dirname(file_local_path)
            if not path.exists(file_dir):
                logger.info("Creating directory %s" % file_dir)
                makedirs(file_dir)

            self.download_file(file_remote_key=key, file_local_path=file_local_path)


    def __repr__(self):
        return "<S3Bucket(name='%s', accessible='%s')>"\
            % (self.bucket_name, self._accessible)

    def __str__(self):
        return self.__repr__()
