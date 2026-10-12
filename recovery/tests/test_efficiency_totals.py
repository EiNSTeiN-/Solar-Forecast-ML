"""Paired efficiency sampling/windows with real stdlib dataclass decoration."""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from pure_reference import classes, functions
from test_shadow_contracts import literal_constants


RELATIVE='extra_features/sfml_stats/core/energy_efficiency.py'


def efficiency_model(side):
    """Construct only proven classes/functions; do not execute a module body."""
    globals_=dict(literal_constants(RELATIVE),datetime=datetime,timedelta=timedelta)
    module=ModuleType('pure_recovery_fixture')
    module.__dict__.update(globals_)
    with patch.dict(sys.modules,{'pure_recovery_fixture':module}):
        totals=dataclass(classes(RELATIVE,'EfficiencyTotals',globals_)[side],slots=True)
        window=dataclass(classes(RELATIVE,'_EfficiencyWindow',globals_)[side],slots=True)
    names=['efficiency_percent','_pv_inverter_input_w','_is_plausible_conversion_sample',
           'build_energy_chain_summary','_build_efficiency_branch','aggregate_efficiency_rows',
           '_integrate_interval_into_windows','_value','_number','_parse_timestamp']
    selected=functions(RELATIVE,names,dict(globals_,EfficiencyTotals=totals,_EfficiencyWindow=window))[side]
    totals.add_sample.__globals__.update(selected)
    window.add_to.__globals__.update(selected)
    return totals,window,selected,globals_


class EfficiencyTotalsTests(unittest.TestCase):
    def test_sample_modes_plausibility_charge_unknown_and_gap_boundaries(self):
        models=[efficiency_model(side) for side in (0,1)]
        baseline=dict(seconds=60,solar_dc_w=1000,inverter_ac_w=900,battery_dc_w=0,
                      grid_to_battery_w=0,battery_configured=True,solar_to_battery_w=None)
        cases=[baseline]
        for key,value in (('seconds',0),('seconds',-1),('seconds',300),('seconds',301),
                          ('inverter_ac_w',None),('inverter_ac_w',1001),('solar_dc_w',0),
                          ('solar_dc_w',None),('battery_dc_w',None),('battery_configured',False),
                          ('battery_dc_w',200),('battery_dc_w',-500),('grid_to_battery_w',100),
                          ('solar_to_battery_w',400),('solar_to_battery_w',0)):
            cases.append(dict(baseline,**{key:value}))
        cases.extend([dict(baseline,solar_dc_w=0,battery_dc_w=-1000),
                      dict(baseline,solar_dc_w=0,battery_dc_w=-500),
                      dict(baseline,solar_dc_w=400,battery_dc_w=-1000)])
        for values in cases:
            outputs=[]
            for totals_type,_,_,_ in models:
                totals=totals_type()
                totals.add_sample(**values)
                outputs.append((asdict(totals),totals.as_dict()))
            self.assertEqual(outputs[1],outputs[0])
        outputs=[]
        for totals_type,_,_,_ in models:
            totals=totals_type()
            for values in cases: totals.add_sample(**values)
            outputs.append((asdict(totals),totals.as_dict()))
        self.assertEqual(outputs[1],outputs[0])

    def test_window_classification_coverage_and_split_intervals(self):
        models=[efficiency_model(side) for side in (0,1)]
        baseline=dict(ac_sensor_seconds=3600,battery_sensor_seconds=3600,valid_seconds=3600,
                      solar_dc_kwh=1,pv_inverter_dc_kwh=1,battery_discharge_dc_kwh=0,ac_kwh=0.9)
        cases=[baseline]
        for key,value in (('valid_seconds',0),('valid_seconds',3239),('valid_seconds',3240),
                          ('ac_sensor_seconds',7200),('battery_sensor_seconds',7200),
                          ('ac_kwh',1.1),('pv_inverter_dc_kwh',0)):
            cases.append(dict(baseline,**{key:value}))
        cases.extend([dict(baseline,pv_inverter_dc_kwh=0,battery_discharge_dc_kwh=1),
                      dict(baseline,pv_inverter_dc_kwh=0.5,battery_discharge_dc_kwh=0.5),
                      dict(baseline,pv_inverter_dc_kwh=0.01,ac_kwh=0.009)])
        for values in cases:
            outputs=[]
            for totals_type,window_type,_,_ in models:
                totals=totals_type()
                window_type(**values).add_to(totals)
                outputs.append(asdict(totals))
            self.assertEqual(outputs[1],outputs[0])
        start=datetime(2026,10,12,0,59,tzinfo=timezone.utc)
        outputs=[]
        row=dict(solar_power_w=1000,inverter_ac_output_w=900,battery_power_dc_w=0,
                 grid_to_battery_w=0,solar_to_battery_w=None)
        for _,_,selected,_ in models:
            windows={}
            selected['_integrate_interval_into_windows'](windows,start=start,
                end=start+timedelta(minutes=3),row=row,battery_configured=True)
            outputs.append({stamp:asdict(window) for stamp,window in windows.items()})
        self.assertEqual(outputs[1],outputs[0])
        self.assertEqual(len(outputs[0]),2)
        self.assertEqual(sum(window['valid_seconds'] for window in outputs[0].values()),180)

    def test_aggregate_sorting_hour_finalization_missing_battery_and_large_gaps(self):
        models=[efficiency_model(side) for side in (0,1)]
        start=datetime(2026,10,12,tzinfo=timezone.utc)
        def rows(minutes,battery=0):
            return [dict(timestamp=(start+timedelta(minutes=minute)).isoformat(),solar_power_w=1000,
                         inverter_ac_output_w=900,battery_power_dc_w=battery,
                         grid_to_battery_w=0,solar_to_battery_w=None) for minute in minutes]
        cases=[[],rows([0]),rows(range(0,65,5)),rows(range(0,75,5)),
               rows(range(0,75,5),None),list(reversed(rows(range(0,75,5)))),
               rows([0,30,60,70]),rows([0,5,5,10,15,20,25,30,35,40,45,50,55,60,65,70])]
        for sample in cases:
            outputs=[asdict(selected['aggregate_efficiency_rows'](sample)) for _,_,selected,_ in models]
            self.assertEqual(outputs[1],outputs[0])
        self.assertEqual(models[1][2]['aggregate_efficiency_rows'](rows(range(0,65,5))).system_sample_seconds,0)
        final=models[1][2]['aggregate_efficiency_rows'](rows(range(0,75,5)))
        self.assertGreater(final.system_sample_seconds,0)

    def test_energy_chain_quality_availability_autarky_and_mapping_errors(self):
        models=[efficiency_model(side) for side in (0,1)]
        baseline=dict(ac_measurement_seconds=3600,system_sample_seconds=3600,
                      system_input_sample_kwh=1,system_output_sample_kwh=0.9,system_efficiency_percent=90,
                      pv_dc_sample_kwh=1,pv_ac_sample_kwh=0.9,inverter_efficiency_percent=90,
                      pv_sample_seconds=3600,battery_dc_sample_kwh=0.5,battery_ac_sample_kwh=0.4,
                      battery_discharge_efficiency_percent=80,battery_sample_seconds=1800,
                      mixed_dc_sample_kwh=0.4,mixed_ac_sample_kwh=0.35,mixed_efficiency_percent=87.5,
                      mixed_sample_seconds=1800)
        cases=[baseline,{}]
        for key,value in (('system_efficiency_percent',None),('system_efficiency_percent',0),
                          ('system_efficiency_percent',101),('system_input_sample_kwh',0),
                          ('system_sample_seconds',1800),('ac_measurement_seconds','unknown')):
            cases.append(dict(baseline,**{key:value}))
        for sample in cases:
            for battery in (False,True):
                for autarky in (None,-1,75,120):
                    outputs=[selected['build_energy_chain_summary'](sample,net_autarky_percent=autarky,
                             elapsed_seconds=3600,battery_available=battery) for _,_,selected,_ in models]
                    self.assertEqual(outputs[1],outputs[0])
        class BrokenRow:
            def __getitem__(self,key): raise KeyError('owned row failure')
        outputs=[]
        for _,_,selected,_ in models:
            try: selected['build_energy_chain_summary'](BrokenRow(),net_autarky_percent=75,
                                                      elapsed_seconds=3600,battery_available=True)
            except KeyError as error: outputs.append((type(error).__name__,str(error)))
        self.assertEqual(outputs[1],outputs[0])


if __name__=='__main__':
    unittest.main()
