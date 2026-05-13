from systems.rawdata import RawData


import pandas as pd

from systems.system_cache import diagnostic, output
from syscore.dateutils import BUSINESS_DAYS_IN_YEAR


# put near top of the file (module scope)
_UNIVERSE_PRICE_PANEL_CACHE = {}
    
class myFuturesRawData(RawData):
    """
    A SubSystem that does futures specific raw data calculations

    Name: rawdata
    """


    @output()
    def skew(self, instrument_code, lookback_days=365):
        """
        Return skew over a given time period
        :param instrument_code:
        :param lookback_days: int
        :return: rolling estimator of skew
        """
        lookback = "%dD" % lookback_days
        perc_returns = self.get_daily_percentage_returns(instrument_code)
        skew = perc_returns.rolling(lookback).skew()

        return skew

    @output()
    def neg_skew(self, instrument_code, lookback_days=365):
        """
        Return skew over a given time period
        :param instrument_code:
        :param lookback_days: int
        :return: rolling estimator of skew
        """
        skew = self.skew(instrument_code, lookback_days=lookback_days)

        return -skew

    @output()
    def kurtosis(self, instrument_code, lookback_days=365):
        """
        Returns kurtosis over historic period
        :param instrument_code: str
        :param lookback_days: int
        :return: rolling estimator of kurtosis
        """

        lookback = "%dD" % lookback_days
        perc_returns = self.get_daily_percentage_returns(instrument_code)
        kurtosis = perc_returns.rolling(lookback).kurt()

        return kurtosis

    @output()
    def get_factor_value_for_instrument(
        self, instrument_code, factor_name="skew", **kwargs
    ):
        """
        Returns the factor value for a given instrument
        :param instrument_code: str
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.Series
        """

        try:
            factor_method = getattr(self, factor_name)
        except:
            msg = "Factor %s is not a method in rawdata stage" % factor_name
            self.log.critical(msg)
            raise Exception(msg)

        factor_value = factor_method(instrument_code, **kwargs)

        return factor_value

    @output()
    def average_factor_value_for_instrument(
        self, instrument_code, factor_name="skew", **kwargs
    ):
        """
        Returns the average factor value for a given instrument
        :param instrument_code: str
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.DataFrame
        """
        # Hard coded otherwise **kwargs can get ugly
        span_years = 15
        factor_value = self.get_factor_value_for_instrument(
            instrument_code, factor_name=factor_name, **kwargs
        )
        average_factor_value = factor_value.ewm(
            BUSINESS_DAYS_IN_YEAR * span_years
        ).mean()

        return average_factor_value

    @diagnostic()
    def factor_values_over_instrument_list(
        self, instrument_list, factor_name="skew", **kwargs
    ):
        """
        Return a dataframe with all factor values in instrument list, useful for calculating averages
        :param instrument_list: list of str
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.DataFrame
        """

        all_factor_values = [
            self.get_factor_value_for_instrument(
                instrument_code, factor_name=factor_name, **kwargs
            )
            for instrument_code in instrument_list
        ]
        all_factor_values = pd.concat(all_factor_values, axis=1)
        all_factor_values.columns = instrument_list

        return all_factor_values

    @diagnostic()
    def factor_values_all_instruments(self, factor_name="skew", **kwargs):
        """
        Return a dataframe with all factor values in it, useful for calculating averages
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.DataFrame
        """

        instrument_list = self.parent.get_instrument_list()
        all_factor_values = self.factor_values_over_instrument_list(
            instrument_list, factor_name=factor_name, **kwargs
        )

        return all_factor_values

    @diagnostic()
    def current_average_factor_values_over_all_assets(
        self, factor_name="skew", **kwargs
    ):
        """
        Return the current average of a factor value
        Used for cross sectional averaging, plus also the long run average
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.DataFrame
        """

        all_factor_values = self.factor_values_all_instruments(
            factor_name=factor_name, **kwargs
        )
        cs_average_all_factors = all_factor_values.ffill().mean(axis=1)

        return cs_average_all_factors

    @diagnostic()
    def historic_average_factor_value_all_assets(self, factor_name="skew", **kwargs):
        """
        Average factor value over all assets
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.Series
        """

        # Hard coded otherwise ugly things can happen with **kwargs mismatch
        span_years = 15
        cs_average_all_factors = self.current_average_factor_values_over_all_assets(
            factor_name=factor_name, **kwargs
        )
        historic_average = cs_average_all_factors.ewm(
            BUSINESS_DAYS_IN_YEAR * span_years
        ).mean()

        return historic_average

    @diagnostic()
    def factor_values_over_asset_class(self, asset_class, factor_name="skew", **kwargs):
        """
        Factors value over an asset class
        :param asset_class: str
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.DataFrame
        """

        instrument_list = self.parent.data.all_instruments_in_asset_class(asset_class)
        all_factor_values = self.factor_values_over_instrument_list(
            instrument_list, factor_name=factor_name, **kwargs
        )

        return all_factor_values

    @diagnostic()
    def current_average_factor_value_over_asset_class(
        self, asset_class, factor_name="skew", **kwargs
    ):
        """
        Return the current average of a factor value in an asset class
        Used for cross sectional averaging
        :param asset_class: str
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.Series
        """

        all_factor_values = self.factor_values_over_asset_class(
            asset_class, factor_name=factor_name, **kwargs
        )
        cs_average_all_factors = all_factor_values.ffill().mean(axis=1)

        return cs_average_all_factors

    @diagnostic()
    def average_factor_value_in_asset_class_for_instrument(
        self, instrument_code, factor_name="skew", **kwargs
    ):
        """
        Return the current average of a factor value in an asset class
        Used for cross sectional averaging
        :param instrument_code: str
        :param factor_name: str, points to method in rawdata
        :param **kwargs: passed to factor_name method
        :return: pd.Series
        """

        asset_class = self.parent.data.asset_class_for_instrument(instrument_code)
        current_avg = self.current_average_factor_value_over_asset_class(
            asset_class, factor_name=factor_name, **kwargs
        )

        return current_avg

    @output()
    def get_demeanded_factor_value(
        self,
        instrument_code,
        factor_name="skew",
        demean_method="average_factor_value_for_instrument",
        **kwargs,
    ):
        """
        :param instrument_code: str
        :param factor_name: str
        :param demean_method: str
        :param kwargs: Arguments passed to factor method (directly and via demean method)
        :return: pd.Series
        """
        try:
            assert demean_method in [
                "current_average_factor_values_over_all_assets",
                "historic_average_factor_value_all_assets",
                "average_factor_value_for_instrument",
                "average_factor_value_in_asset_class_for_instrument",
            ]
        except:
            self.log.error("Demeanding method %s is not allowed")

        try:
            demean_function = getattr(self, demean_method)
        except:
            msg = (
                "Demeaning function %s does not exist in rawdata stage" % demean_method
            )
            self.log.critical(msg)
            raise Exception(msg)

        # Get demean value
        if demean_method in [
            "current_average_factor_values_over_all_assets",
            "historic_average_factor_value_all_assets",
        ]:
            # instrument code not needed
            demean_value = demean_function(factor_name=factor_name, **kwargs)
        else:
            demean_value = demean_function(
                instrument_code, factor_name=factor_name, **kwargs
            )

        # Get raw factor value
        factor_value = self.get_factor_value_for_instrument(
            instrument_code, factor_name=factor_name, **kwargs
        )

        # Line them up
        demean_value = demean_value.reindex(factor_value.index)
        demean_value = demean_value.ffill()

        demeaned_value = factor_value - demean_value

        return demeaned_value

    @output()
    def get_rawdata_object(self, instrument_code=None):
        """
        Return the rawdata object itself for rules that need direct access.
        Used by self-contained rules like PCA alpha.

        :param instrument_code: str (ignored, but required for pysystemtrade interface)
        :return: self (the rawdata object)
        """
        return self
    
    # Factor analysis methods that need a price panel
    # ---------------------------------------------------
    @output()
    def get_universe_price_panel(self, instrument_code=None) -> pd.DataFrame:
        """
        Wide daily price panel on the UNION trading calendar.
        Columns = instrument codes in the configured trading universe.
        `instrument_code` is ignored (framework passes it).
        """
        import pandas as pd

        # cache key: tie to data object + exact instrument list
        instruments = list(self.parent.get_instrument_list())
        cache_key = (id(self.parent.data), tuple(sorted(instruments)))

        # module-level memo (survives per-instrument PST caching)
        panel = _UNIVERSE_PRICE_PANEL_CACHE.get(cache_key)
        if panel is not None and not panel.empty:
            # optional: .copy() to avoid accidental mutation by callers
            return panel  # or: panel.copy()

        if not instruments:
            return pd.DataFrame()

        # Build union index across all instrument series
        idxs = []
        series_by_instr = {}
        for instr in instruments:
            try:
                s = self.get_daily_prices(instr)
                if len(s) > 0:
                    series_by_instr[instr] = s
                    idxs.append(s.index)
            except Exception:
                continue

        if not series_by_instr:
            return pd.DataFrame()

        # Union calendar + align + ffill
        union_idx = pd.DatetimeIndex(sorted(set().union(*idxs)))
        panel = (
            pd.DataFrame({instr: series_by_instr[instr].reindex(union_idx) for instr in series_by_instr})
            .sort_index()
            .ffill()
        )

        _UNIVERSE_PRICE_PANEL_CACHE[cache_key] = panel
        return panel

    @output()
    def get_universe_return_panel(self, instrument_code=None, winsor_abs=1.0):
        """
        Wide daily **returns** panel on the UNION trading calendar.
        Built from get_universe_price_panel() to guarantee coverage.
        - Treat non-positive prices as missing.
        - Winsorise returns to kill crazy ticks.
        """
        import numpy as np
        import pandas as pd

        panel = self.get_universe_price_panel()
        if panel is None or panel.empty:
            return pd.DataFrame()

        # treat <=0 as missing (futures stitches can go 0/negative)
        panel = panel.where(panel > 0)

        # simple returns
#        ret = panel.pct_change()
        ret = panel.pct_change(fill_method=None)


        # winsorise to keep outliers from wrecking conditioning
        if winsor_abs is not None:
            ret = ret.clip(-winsor_abs, winsor_abs)

        # if any inf slipped in, nuke to NaN
        ret = ret.replace([np.inf, -np.inf], np.nan)

        return ret



if __name__ == "__main__":
    import doctest

    doctest.testmod()
