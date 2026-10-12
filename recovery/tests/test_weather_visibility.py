"""Paired visibility and transmission boundaries; no weather integration loads.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
from enum import Enum
import math
import unittest
from pure_reference import functions


class WeatherVisibilityTests(unittest.TestCase):
    def test_visibility_validity_priority_contradictions_and_transmission(self):
        cloud_type = Enum('OwnedCloud', {'CLEAR':'clear','CIRRUS':'cirrus','FAIR':'fair',
            'MIXED':'mixed','STRATUS':'stratus','OVERCAST':'overcast','SNOW':'snow',
            'FOG':'fog','FOG_LIGHT':'fog_light'})
        globals_ = {'math':math,'VISIBILITY_CONTRADICTION_LOW_M':1000.0,
            'VISIBILITY_CONTRADICTION_HIGH_M':5000.0,
            'VISIBILITY_SOURCE_UNSUFFIXED':'open_meteo','VISIBILITY_SOURCE_ICON_D2':'icon_d2',
            'VISIBILITY_SOURCE_ICON_EU':'icon_eu','VISIBILITY_SOURCE_ECMWF_IFS025':'ecmwf_ifs025',
            'TRANSMISSION_TAU_BY_CLOUD_TYPE':dict(clear=1.0,cirrus=0.8,fair=0.5,mixed=0.35,
                stratus=0.2,overcast=0.1,snow=0.02,fog=0.5,fog_light=0.7)}
        left,right = functions('data/weather_types.py',
            ('_finite_nonneg_visibility','_visibility_models_contradict',
             'resolve_open_meteo_visibility','cloud_to_transmission'),globals_,
            defaults={'cloud_to_transmission':(cloud_type.MIXED,)},
            kwdefaults={'resolve_open_meteo_visibility':dict(unsuffixed=None,icon_d2=None,
                                                            icon_eu=None,ecmwf_ifs025=None)})
        for value in (None,'invalid','1000',-1,0,3,math.nan,math.inf):
            self.assertEqual(right['_finite_nonneg_visibility'](value),left['_finite_nonneg_visibility'](value))
        for a,b in ((500,6000),(6000,500),(1000,6000),(500,5000),(3000,3000),
                    (math.nan,500),(500,math.inf)):
            self.assertEqual(right['_visibility_models_contradict'](a,b),left['_visibility_models_contradict'](a,b))
        for values in ((None,None,None,None),(2000,500,6000,3000),(None,500,6000,3000),
                       (None,6000,500,3000),(None,2000,3000,4000),
                       ('invalid',None,2500,3000),(None,math.nan,math.inf,3500)):
            options=dict(zip(('unsuffixed','icon_d2','icon_eu','ecmwf_ifs025'),values))
            self.assertEqual(right['resolve_open_meteo_visibility'](**options),
                             left['resolve_open_meteo_visibility'](**options))
        self.assertEqual(right['resolve_open_meteo_visibility'](icon_d2=500,icon_eu=6000),(6000.0,'icon_eu'))
        self.assertEqual(right['resolve_open_meteo_visibility'](icon_d2=6000,icon_eu=500),(6000.0,'icon_d2'))
        for cloud in cloud_type:
            for percent in (-10,0,20,50,100,150):
                self.assertEqual(right['cloud_to_transmission'](percent,cloud),
                                 left['cloud_to_transmission'](percent,cloud))


if __name__ == '__main__':unittest.main()
