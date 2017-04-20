"""Module containing class to build feature matrices for prediction.
"""
import logging
from datetime import datetime
import numpy as np
import pandas as pd

from api_etl.utils_misc import get_paris_local_datetime_now, DateConverter
from api_etl.query import DBQuerier

logger = logging.getLogger(__name__)
pd.options.mode.chained_assignment = None


class DayMatrixBuilder():
    """ Build the X matrix.

    For each trip:
    - determine status: pending, rolling, finished. We will
    only consider rolling trips. 0 (pending) -> 1 (finished).
    - compute last observed delay (for those rolling 0<x<1)

    For each line:
    - compute current line status: median delays over last 10 min
    - compute number of currently rolling trains OR number of train stops
    during last 15 min (easier)
    - for each station: compute current status on this line:
        - last observed delay, to begin (or median of last 10 min)

    Select all stop-times (day_train_num/station_id):
    - filter those not passed yet
    - filter those that are part of rolling trips
    For each stop-time: add following features:
    - compute difference of sequence between last observed stop and this one
    - compute difference of scheduled time between last observed stop and this
    one.
    - add last observed delay of trip
    - add last observed delay of station
    # - add api-predicted delay in comparison to schedule (after)

    """

    def __init__(self, day=None, time=None, df=None):
        """ Must provide day and time, else it will be set to now.

        Still "beta" functionality: provide df directly:
        You can provide directly a df instead of performing all queries, but
        be aware that the TripState will be based on time given when computing
        trip state (and not time used in this __init__).
        """

        if day and time:
            full_str_dt = "%s%s" % (day, time)
            self.datetime = datetime.strptime(full_str_dt, "%Y%m%d%H:%M:%S")
            self.day = day
            self.time = time
        else:
            self.datetime = get_paris_local_datetime_now()
            self.day = self.datetime.strftime("%Y%m%d")
            self.time = self.datetime.strftime("%H:%M:%S")

        if isinstance(df, pd.DataFrame):
            self.df = df
        else:
            self.querier = DBQuerier(yyyymmdd=self.day)
            # Get schedule
            self.stops_results = self.querier.stops_of_day(self.day)
            logger.info("Schedule queried.")
            # Perform realtime queries and compute states
            self.stops_results.batch_realtime_query(self.day)
            logger.info("RealTime queried.")
            # Export flat dict as dataframe
            self._initial_df = pd\
                .DataFrame(self.stops_results.get_flat_dicts())
            self.df = self._initial_df.copy()

        # Compute rest
        logger.info("Performing matrix features computations.")
        self._clean_initial_df()
        self._compute_trip_state()
        self._trip_level()
        self._line_level()

    def _clean_initial_df(self):
        self.df.replace("Unknown", np.nan, inplace=True)

        cols_to_num = ["StopTime_stop_sequence"]

        for col in cols_to_num:
            self.df[col] = pd.to_numeric(self.df[col], errors="coerce")

    def _compute_trip_state(self):
        """Computes:
        - TripState_at_datetime: datetime
        - TripState_passed_schedule: Bool
        - TripState_real_passage_vs_prediction_time_diff: int (seconds)
        - TripState_passed_realtime: Bool
        - TripState_observed_delay: int (seconds)
        - TripState_expected_delay: int (seconds)
        """

        self.df["TripState_at_datetime"] = self.datetime.strftime(
            "%Y%m%d-%H:%M:%S")

        self.df["TripState_passed_schedule"] = self.df\
            .apply(lambda x: DateConverter(
                special_date=self.day,
                special_time=x["StopTime_departure_time"]
            )
                .compute_delay_from(dt=self.datetime),
                axis=1
        )\
            .apply(lambda x: (x >= 0))

        # Time between observed datetime (for which we compute the prediction
        # features matrix), and stop times observed passages (only for observed
        # passages).
        self.df["TripState_real_passage_vs_prediction_time_diff"] = self\
            .df[self.df.RealTime_expected_passage_time.notnull()]\
            .apply(lambda x: DateConverter(
                special_date=x["RealTime_expected_passage_day"],
                special_time=x["RealTime_expected_passage_time"]
            )
                .compute_delay_from(dt=self.datetime),
                axis=1
        )

        self.df["TripState_passed_realtime"] = self\
            .df[self.df.TripState_real_passage_vs_prediction_time_diff
                .notnull()]\
            .TripState_real_passage_vs_prediction_time_diff\
            .apply(lambda x: (x >= 0))

        # TripState_observed_delay
        self.df["TripState_observed_delay"] = self\
            .df[self.df.TripState_passed_realtime == True]\
            .apply(
                lambda x: DateConverter(
                    special_date=x["RealTime_expected_passage_day"],
                    special_time=x["RealTime_expected_passage_time"]
                )
                .compute_delay_from(
                    special_date=self.day,
                    special_time=x["StopTime_departure_time"]
                ),
                axis=1
        )

        # TripState_expected_delay
        self.df["TripState_expected_delay"] = self\
            .df[self.df.TripState_passed_realtime != True][self.df.RealTime_expected_passage_time.notnull()]\
            .apply(
                lambda x: DateConverter(
                    special_date=x["RealTime_expected_passage_day"],
                    special_time=x["RealTime_expected_passage_time"]
                )
                .compute_delay_from(
                    special_date=self.day,
                    special_time=x["StopTime_departure_time"]
                ),
                axis=1
        )

    def _trip_level(self):
        """Compute trip level information:
        - trip_status: 0<=x<=1: proportion of passed stations at time
        - total_sequence: number of stops scheduled for this trip
        - last_sequence_number: last observed stop sequence for this trip at
        time
        - last_observed_delay
        """
        # Trips total number of stops
        trips_total_number_stations = self.df\
            .groupby("Trip_trip_id")["TripState_passed_schedule"].count()
        trips_total_number_stations.name = "total_sequence"
        self.df = self.df.join(trips_total_number_stations, on="Trip_trip_id")

        # Trips status at time
        trips_number_passed_stations = self.df\
            .groupby("Trip_trip_id")["TripState_passed_schedule"].sum()
        trips_status = trips_number_passed_stations \
            / trips_total_number_stations
        trips_status.name = "trip_status"
        self.trips_status = trips_status
        self.df = self.df.join(trips_status, on="Trip_trip_id")

        # Trips last observed stop_sequence
        self.last_sequence_number = self\
            .df[(self.df.trip_status < 1) & (self.df.trip_status > 0) & (self.df.TripState_passed_realtime == True)]\
            .groupby("Trip_trip_id")["StopTime_stop_sequence"].max()
        self.last_sequence_number.name = "last_sequence_number"
        self.df = self.df.join(self.last_sequence_number, on="Trip_trip_id")

        # Compute number of stops between last observed station and predicted
        # station.
        self.df["sequence_diff"] = self.df.StopTime_stop_sequence - \
            self.df.last_sequence_number

        # Trips last observed delay
        self.last_observed_delay = self.df[self.df.last_sequence_number == self.df.StopTime_stop_sequence][
            ["Trip_trip_id", "TripState_observed_delay"]]
        self.last_observed_delay.set_index("Trip_trip_id", inplace=True)
        self.last_observed_delay.columns = ["last_observed_delay"]
        self.df = self.df.join(self.last_observed_delay, on="Trip_trip_id")

        # Trips last observed scheduled departure time
        # useful to know how much time was scheduled between stations
        self.last_observed_scheduled_dep_time = self\
            .df[self.df.last_sequence_number ==
                self.df.StopTime_stop_sequence][
                ["Trip_trip_id", "StopTime_departure_time"]]
        self.last_observed_scheduled_dep_time\
            .set_index("Trip_trip_id", inplace=True)
        self.last_observed_scheduled_dep_time.columns = [
            "last_observed_scheduled_dep_time"]
        self.df = self.df\
            .join(self.last_observed_scheduled_dep_time, on="Trip_trip_id")

        # Compute number of seconds between last observed passed trip scheduled
        # departure time, and departure time of predited station
        self.df["stations_scheduled_trip_time"] = self\
            .df[self.df.last_observed_scheduled_dep_time.notnull()]\
            .apply(lambda x: DateConverter(
                special_date=self.day,
                special_time=x["StopTime_departure_time"]
            )
                .compute_delay_from(
                    special_date=self.day,
                    special_time=x["last_observed_scheduled_dep_time"]
            ),
                axis=1
        )

    def _line_level(self):
        """ Computes line level information:
        - median delay on line on last n seconds
        - median delay on line station on last n seconds
        - number of currently rolling trips on line

        Requires time to now (_add_time_to_now_col).
        """
        # Compute delays on last n seconds
        secs = 600

        # Line aggregation
        self.line_median_delay = self\
            .df[(self.df.TripState_real_passage_vs_prediction_time_diff > -secs) &
                self.df.TripState_real_passage_vs_prediction_time_diff <= 0]\
            .groupby("Route_route_short_name").TripState_observed_delay.median()
        self.line_median_delay.name = "line_median_delay"
        self.df = self.df.join(
            self.line_median_delay,
            on="Route_route_short_name")

        # Line and station aggregation
        # same station can have different values given on which lines it
        # is located.
        self.line_station_median_delay = self\
            .df[(self.df.TripState_real_passage_vs_prediction_time_diff > -secs) &
                self.df.TripState_real_passage_vs_prediction_time_diff <= 0]\
            .groupby(["Route_route_short_name", "Stop_stop_id"])\
            .TripState_observed_delay.median()
        self.line_station_median_delay.name = "line_station_median_delay"
        self.df = self.df.join(
            self.line_station_median_delay,
            on=["Route_route_short_name", "Stop_stop_id"])

        # Number of currently rolling trips
        self.rolling_trips_on_line = self\
            .df.query("trip_status>0 & trip_status<1")\
            .groupby("Route_route_short_name")["Trip_trip_id"]\
            .count()
        self.rolling_trips_on_line.name = "rolling_trips_on_line"
        self.df = self.df.join(
            self.rolling_trips_on_line,
            on="Route_route_short_name")

    def get_rolling_trips(self, status=True):
        r = self\
            .trips_status[(self.trips_status > 0) & (self.trips_status < 1)]
        if status:
            return r
        else:
            return r.index

    def stats(self):
        # StopTimes
        # - number of stop times scheduled
        # - number of stop times passed schedule
        # - number of stop times passed realtime
        # - number of stop times found not passed realtime
        #        (currently on boards)
        #
        # Trips
        # - schedule: number of trips: not begun, rolling, finished
        # - realtime: number of trips with at least one stop observed
        #
        print("\nTRIPS")
        print("Based on schedule:")
        print("Number of trips on day: %s" %
              len(self.df.Trip_trip_id.unique()))
        print("Number of trips currently rolling: %s" %
              self.df.query(
                  "(trip_status > 0) & (trip_status < 1)")
              .Trip_trip_id.unique().shape[0])

        print("\nSTOPTIMES")
        print("Number of stop times scheduled: %s" %
              self.df.Trip_trip_id.count())
        print("Number of stop times that passed based on schedule: %s" %
              self.df.TripState_passed_schedule.sum())
        print("Number of stop times that passed based on observations: %s" %
              self.df.TripState_passed_realtime.sum())
        print("Number of stop times observed but not passed yet (still on boards): %s" %
              (self.df.TripState_passed_realtime == False).sum())

        print("\nPREDICTABLE STOPTIMES")
        print("Total number of stops for rolling trips (based on schedule): %s" %
              self.df
              .query("(trip_status > 0) & (trip_status < 1)")
              .Trip_trip_id.count())

        print("Rolling-trips' stops not passed yet (based on schedule), those for which we could make a prediction if we found information about precedent stops: %s" %
              self.df
              .query("(trip_status > 0) & (trip_status < 1) &(TripState_passed_schedule==False)")
              .Trip_trip_id.count()
              )

        print("Rolling-trips' stops already passed (based on schedule), which is the information on which we should rely for our predictions for rolling trips: %s" % self.df
              .query("(trip_status > 0) & (trip_status < 1) &(TripState_passed_schedule==True)")
              .Trip_trip_id.count())

        print("For these, how many we observed through api: %s" % self.df
              .query("(trip_status > 0) & (trip_status < 1) &(TripState_passed_realtime==True)")
              .Trip_trip_id.count())

    def get_predictable(self, strict=False, col_filter=True):
        """Return predictable stop times.
        """
        # Basic Conditions:
        # - trip_status stricly between 0 and 1,
        # - has not passed yet schedule (not True)
        # - has not passed yet realtime (not True, it can be Nan or False)

        rdf = self.df.query(
            "trip_status < 1 & trip_status > 0 & TripState_passed_schedule !=\
            True & TripState_passed_realtime != True")

        if strict:
            # Strict Conditions:
            # - found last_observed_delay
            # - found line_station_delay

            rdf = rdf.query(
                "last_observed_delay.notnull() &\
                line_station_median_delay.notnull()")

        self.filtered_cols = [
            # Identification basics
            "Trip_trip_id",
            "Stop_stop_id",
            # Identification names
            "Route_route_short_name",
            "Stop_stop_name",
            # Features
            "last_observed_delay",
            "line_station_median_delay",
            "line_median_delay",
            "sequence_diff",
            "stations_scheduled_trip_time",
            "rolling_trips_on_line",
            # Checking if alright
            "StopTime_departure_time",
            "RealTime_expected_passage_time",
            "RealTime_data_freshness",
            "TripState_passed_realtime",
            "TripState_observed_delay",
            "TripState_expected_delay",
            # If passed_realtime is False (not nan), delay is predicted delay
        ]

        if col_filter:
            rdf = rdf[self.filtered_cols]

        return rdf

    def missing_data_per(self, per="Stop_stop_name"):
        # per can be also "Stop_stop_id", "Route_route_short_name"
        md = self.df.copy(deep=True)
        md["rt"] = ~md["RealTime_day_train_num"].isnull()
        md["rt"] = md.rt.apply(int)
        agg = md.groupby(per)["rt"].mean()
        return agg


def save_day_matrices_as_csv(start, end, data_path=None):
    """ Sart and end included
    """
    dti = pd.date_range(start=start, end=end, freq="D")
    days = dti.map(lambda x: x.strftime("%Y%m%d")).tolist()

    for day in days:
        mat = DayMatrixBuilder(day)
        mat.df.to_csv("%s%s" % (day, ".csv"))