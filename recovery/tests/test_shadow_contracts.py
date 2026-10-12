"""Verified shadow forecast/observation functions with controlled dependencies."""
import ast
from collections import defaultdict
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

from pure_reference import functions


def literal_constants(relative):
    """Read only literal declarations, never execute an integration module."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'verified_units' / relative).read_text())
    values = {}
    def literal(node):
        if isinstance(node, ast.Constant): return node.value
        if isinstance(node, ast.Name) and node.id in values: return values[node.id]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return {ast.List:list, ast.Tuple:tuple, ast.Set:set}[type(node)](literal(item) for item in node.elts)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'frozenset' and len(node.args) == 1 and not node.keywords):
            return frozenset(literal(node.args[0]))
        raise ValueError('Not a literal declaration')
    for statement in tree.body:
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)):
            try: values[statement.targets[0].id] = literal(statement.value)
            except ValueError: pass
    return values


class ShadowForecastTests(unittest.TestCase):
    relative = 'core/core_shadow_forecast.py'

    def functions(self):
        return functions(self.relative, ['build_shadow_forecast_context', '_inactive_context',
                                        '_fraction', '_percent', 'apply_shadow_to_calibrated_physics'],
                         dict(literal_constants(self.relative), math=math,
                              ShadowForecastContext=lambda **values:SimpleNamespace(**values)),
                         kwdefaults={'_inactive_context':{'pattern_type':'unknown'}})

    def test_context_rejection_thresholds_seasonal_blending_and_sensorless_cap(self):
        original, recovered = self.functions()
        baseline = {'pattern_type':'frequent', 'sample_count':7, 'clear_days':1,
                    'confidence':0.8, 'pct_building_tree':0.8,
                    'shadow_occurrence_rate':0.8, 'avg_shadow_percent':60.0}
        cases = [(None, None), ({}, None), (baseline, None)]
        for key, value in (('pattern_type','unknown'), ('sample_count',6), ('clear_days',0),
                           ('confidence',0.49), ('pct_building_tree',0.59),
                           ('shadow_occurrence_rate',0.0), ('confidence',float('nan')),
                           ('confidence',2.0), ('avg_shadow_percent',200.0),
                           ('observation_class','sensorless_candidate')):
            cases.append((dict(baseline, **{key:value}), None))
        seasonal = {'dominant_cause':'building_tree_obstruction', 'sample_count':3,
                    'confidence':0.7, 'shadow_occurrence_rate':0.4, 'avg_shadow_percent':20.0}
        cases.append((baseline, seasonal))
        for key, value in (('dominant_cause','weather_clouds'), ('sample_count',2), ('confidence',0.49)):
            cases.append((baseline, dict(seasonal, **{key:value})))
        for hourly, season in cases:
            left = original['build_shadow_forecast_context'](hourly, season, scope='owned-group')
            right = recovered['build_shadow_forecast_context'](hourly, season, scope='owned-group')
            self.assertEqual(vars(right), vars(left))

    def test_calibrated_physics_preserves_caps_disabled_context_and_conversion_errors(self):
        original, recovered = self.functions()
        for eligible in (False, True):
            for calibrated, raw, calibration in ((10,10,1), (10,20,1), (10,10,3),
                                                 (-1,10,1), (10,-1,1), (10,10,-1),
                                                 ('10','8','0.8'), (None,8,1), (10,None,1)):
                outcomes = []
                for selected in (original, recovered):
                    context = SimpleNamespace(eligible=eligible, physics_factor=0.6)
                    try:
                        value = selected['apply_shadow_to_calibrated_physics'](
                            calibrated_kwh=calibrated, raw_kwh=raw,
                            global_calibration_factor=calibration, context=context)
                    except (TypeError, ValueError) as error: value = type(error).__name__, str(error)
                    outcomes.append(value)
                self.assertEqual(outcomes[1], outcomes[0])


class ShadowObservationTests(unittest.TestCase):
    relative = 'data/data_shadow_observation.py'

    def functions(self):
        names = ['_finite','median','elevation_bin','azimuth_span_deg','weather_is_clear_enough',
                 'pyranometer_is_implausible','quantile','ceiling_for_bin','build_geometry_ceilings',
                 'classify_observation_class','is_local_learning_cause','shadow_detected_from_observation',
                 'classify_sensorless_hour']
        return functions(self.relative,names,dict(literal_constants(self.relative),
                                                math=math,defaultdict=defaultdict))

    def test_numeric_geometry_boundaries_weather_gates_and_dead_sensor_checks(self):
        original, recovered = self.functions()
        calls = []
        for values in ([],[1],[1,3],[4,1,2],[0,2,5,9]):
            calls.append(('median',(values,)))
            for fraction in (-1,0,0.5,1,2): calls.append(('quantile',(values,fraction)))
        for value in (None,True,False,'invalid',float('nan'),float('inf'),19.9,20,30):
            calls.extend([('_finite',(value,)),('elevation_bin',(value,))])
        for azimuths in ([],[10],[350,10],[0,90,180,270],[float('nan'),10,80]):
            calls.append(('azimuth_span_deg',(azimuths,)))
        for cloud,elevation in ((None,30),(0,19.9),(15,20),(15.1,20),(float('nan'),30)):
            for rain in (None,0,0.1): calls.append(('weather_is_clear_enough',(cloud,rain,elevation)))
        for arguments in ((0,200,0.25,1),(5,200,0.25,1),(5.1,200,0.25,1),
                          (0,199,0.25,1),(0,200,0.24,1),(None,200,0.25,1),(0,200,1,0)):
            calls.append(('pyranometer_is_implausible',arguments))
        calls.extend([('ceiling_for_bin',([0.5]*11,list(range(0,110,10)))),
                      ('ceiling_for_bin',([0.5]*12,[10]*12)),
                      ('ceiling_for_bin',([0.5]*12,list(range(0,120,10))))])
        for name,arguments in calls:
            self.assertEqual(recovered[name](*arguments),original[name](*arguments))
        samples = [dict(sun_elevation_deg=25,sun_azimuth_deg=azimuth,actual_kwh=0.7,theoretical_max_kwh=1)
                   for azimuth in range(0,120,10)]
        samples.extend([{},dict(sun_elevation_deg=19,sun_azimuth_deg=20,actual_kwh=1,theoretical_max_kwh=1),
                        dict(sun_elevation_deg=35,sun_azimuth_deg=40,actual_kwh=1,theoretical_max_kwh=0)])
        self.assertEqual(recovered['build_geometry_ceilings'](samples),original['build_geometry_ceilings'](samples))

    def test_observation_classes_learning_routes_and_sensorless_residuals(self):
        original,recovered = self.functions()
        for fusion in (None,'solar_radiation_sensor','cloud_based','peer_group','unknown'):
            for cause in (None,'building_tree_obstruction','possible_obstruction','normal_variation'):
                for evaluable, candidate in ((False,False),(True,False),(True,True)):
                    for group in ('_system_','owned-group'):
                        values=dict(fusion_mode=fusion,ensemble_mode='normal',root_cause=cause,
                                    group_name=group,evaluable=evaluable,shadow_candidate=candidate)
                        self.assertEqual(recovered['classify_observation_class'](**values),
                                         original['classify_observation_class'](**values))
                        observation=original['classify_observation_class'](**values)
                        learning=dict(root_cause=cause,observation_class=observation,fusion_mode=fusion,
                                      group_name=group,shadow_detected=candidate)
                        self.assertEqual(recovered['is_local_learning_cause'](**learning),
                                         original['is_local_learning_cause'](**learning))
                        self.assertEqual(recovered['shadow_detected_from_observation'](observation),
                                         original['shadow_detected_from_observation'](observation))
        baseline=dict(cloud_cover=0,precipitation_mm=0,sun_elevation_deg=30,actual_kwh=0.5,
                      theoretical_max_kwh=1,ceiling_ready=True,ceiling_ratio=1,residual_threshold=0.3)
        cases=[baseline]
        for key,value in (('cloud_cover',None),('cloud_cover',15),('cloud_cover',15.1),
                          ('precipitation_mm',0.1),('sun_elevation_deg',19.9),('actual_kwh',None),
                          ('actual_kwh',0.7),('actual_kwh',0.8),('ceiling_ready',False),('ceiling_ratio',None)):
            cases.append(dict(baseline,**{key:value}))
        for values in cases:
            self.assertEqual(recovered['classify_sensorless_hour'](**values),
                             original['classify_sensorless_hour'](**values))


if __name__ == '__main__':
    unittest.main()
