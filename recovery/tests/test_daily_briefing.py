"""Paired briefing behavior with owned clocks, imports, databases and services."""
import asyncio
import copy
from datetime import datetime,time,timedelta,timezone
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from pure_reference import classes,functions
from test_isolated_initialization import logger_for


RELATIVE='services/service_daily_briefing.py'
NOW=datetime(2026,10,12,9,tzinfo=ZoneInfo('America/Toronto'))


def briefing(side,events,*,database=None,weather=None,dev=False,now=NOW,services=None,importer=None):
    """Instantiate a fully proved class without executing its integration module."""
    def clock(): events.append(('now',)); return now
    globals_=dict(datetime=datetime,time=time,timedelta=timedelta,_LOGGER=logger_for(events),
                  dt_util=SimpleNamespace(now=clock),_FINAL_MORNING_FORECAST_OFFSET=timedelta(minutes=45))
    selected=functions(RELATIVE,['_published_dev_mode_active'],globals_)[side]
    globals_.update(selected)
    options={'builtins_overrides':{'__import__':importer}} if importer is not None else {}
    cls=classes(RELATIVE,'DailyBriefingService',globals_,**options)[side]
    hass=SimpleNamespace(data={'solar_forecast_ml':{'dev_mode':{'active':dev}}},services=services)
    coordinator=SimpleNamespace(data_manager=SimpleNamespace(_db_manager=database),
                                weather_pipeline_manager=SimpleNamespace(
                                    weather_corrector=SimpleNamespace(_current_weather_cache=weather)),
                                ai_predictor=SimpleNamespace(_model_info={'r2_score':0.9}))
    return cls(hass,coordinator)


def outcome(callback):
    try: return callback()
    except (ValueError,TypeError,KeyError,IndexError,RuntimeError,asyncio.CancelledError) as error:
        return type(error).__name__,str(error)


class DailyBriefingTests(unittest.TestCase):
    def test_weather_messages_quotes_and_thresholds_preserve_all_localized_text(self):
        models=[briefing(side,[]) for side in (0,1)]
        clouds=(None,0,9.9,10,19.9,20,24.9,25,39.9,40,49.9,50,59.9,60,74.9,75,79.9,80,89.9,90,100)
        for language in ('de','en','fr'):
            for cover in clouds:
                for prediction in (0,0.5,1,2,5,10,15,20):
                    results=[(model._interpret_weather(prediction,cover,language),
                              model._get_weather_description(cover,language),
                              model._get_closing_message(prediction,cover,language),
                              model._get_daily_quote(cover,prediction,278,language)) for model in models]
                    self.assertEqual(results[1],results[0])

    def test_datetime_parse_final_lock_boundaries_and_cached_sunrise(self):
        for value in (None,'','invalid','2026-10-12T07:15:00','2026-10-12T11:15:00Z',
                      datetime(2026,10,12,7,15),datetime(2026,10,12,11,15,tzinfo=timezone.utc)):
            results=[]
            for side in (0,1):
                events=[]
                model=briefing(side,events)
                result=model._parse_datetime(value)
                due=NOW.replace(hour=7,minute=15)
                locked=[outcome(lambda enabled=enabled:model._is_final_morning_forecast_ready(
                         {'locked':enabled,'locked_at':value},due)) for enabled in (False,True)]
                results.append((result,locked,events))
            self.assertEqual(results[1],results[0])
        for sunrise in (None,'','invalid','2026-10-12T06:00:00-04:00'):
            results=[]
            for side in (0,1):
                events=[]
                class Database:
                    async def fetchone(self,sql,parameters):
                        events.append(('query',sql,parameters)); return (sunrise,)
                model=briefing(side,events,database=Database())
                result=asyncio.run(model._get_final_morning_forecast_due_at('2026-10-12'))
                results.append((result,events))
            self.assertEqual(results[1],results[0])
        for locked_at in ('2026-10-12T07:14:59-04:00','2026-10-12T07:15:00-04:00',
                          '2026-10-12T07:15:01-04:00'):
            models=[briefing(side,[]) for side in (0,1)]
            values={'locked':True,'locked_at':locked_at}
            due=NOW.replace(hour=7,minute=15)
            self.assertEqual(models[1]._is_final_morning_forecast_ready(values,due),
                             models[0]._is_final_morning_forecast_ready(values,due))

    def test_database_readers_empty_unready_errors_cancellation_and_weather_cache(self):
        for scenario in ('normal','missing-db','empty','unready','zero-actual','db-error','cancelled','cache'):
            results=[]
            for side in (0,1):
                events=[]
                class Database:
                    async def fetchone(self,sql,parameters):
                        events.append(('fetchone',sql,parameters))
                        if scenario=='db-error': raise RuntimeError('owned database failure')
                        if scenario=='cancelled': raise asyncio.CancelledError('owned database cancellation')
                        if scenario=='empty': return None
                        if 'astronomy_cache' in sql: return (None,)
                        if 'daily_forecasts' in sql: return (5,'owned',scenario!='unready','2026-10-12T07:30:00-04:00')
                        if 'daily_summaries' in sql: return (0 if scenario=='zero-actual' else 4,5,80)
                        if 'AVG(pw.clouds)' in sql: return (20.5,15.4)
                        if 'ORDER BY prediction_kwh' in sql: return (12,1.2,'owned')
                        raise AssertionError('Unexpected query')
                database=None if scenario=='missing-db' else Database()
                weather={'temperature':16,'clouds':10,'wind':2,'humidity':40} if scenario=='cache' else None
                model=briefing(side,events,database=database,weather=weather)
                values=[outcome(lambda method=method:asyncio.run(getattr(model,method)())) for method in
                        ('_get_today_forecast_data','_get_yesterday_actual_data','_get_today_weather_data','_get_peak_hour')]
                results.append((values,events))
            self.assertEqual(results[1],results[0])
        self.assertTrue(results[1][0][0]['locked'])

    def test_astronomy_import_cache_and_handler_routes_are_owned(self):
        for scenario in ('normal','empty','import-error','cache-error'):
            results=[]
            for side in (0,1):
                events=[]
                class Cache:
                    def get_day_data(self,target):
                        events.append(('day',target))
                        if scenario=='cache-error': raise RuntimeError('owned cache failure')
                        if scenario=='empty': return None
                        return {'sunrise_time':'06:00','sunset_time':'18:00','solar_noon':'12:00','day_length_hours':12}
                def importer(name,globals=None,locals=None,fromlist=(),level=0):
                    self.assertEqual(name,'astronomy.astronomy_cache_manager')
                    self.assertEqual(fromlist,('get_cache_manager',)); self.assertEqual(level,2)
                    events.append(('import',name,fromlist,level))
                    if scenario=='import-error': raise ImportError('owned import failure')
                    return SimpleNamespace(get_cache_manager=lambda:Cache())
                model=briefing(side,events,importer=importer)
                value=asyncio.run(model._get_astronomy_data())
                results.append((value,events))
            self.assertEqual(results[1],results[0])
            if scenario=='normal': self.assertEqual(results[1][0]['daylight_hours'],12)

    def test_learning_exclusions_row_fallback_and_shadow_summaries(self):
        for language in ('de','en'):
            for scenario in ('normal','empty','db-error'):
                results=[]
                for side in (0,1):
                    events=[]
                    class Database:
                        async def fetchall(self,sql,parameters):
                            events.append(('fetchall',sql,parameters))
                            if scenario=='db-error': raise RuntimeError('owned database failure')
                            if scenario=='empty': return []
                            if 'shadow_learning_history' in sql:
                                return [(8,1,'building_tree_obstruction',0.3,0.8),
                                        (9,1,'weather_clouds',0.7,0.6)]
                            return [(None,0,None,1),('unexpected_snow',0,None,0),
                                    (None,1,'battery_full',0),(None,1,None,0),(None,0,None,0)]
                    model=briefing(side,events,database=Database(),dev=True)
                    values=[outcome(lambda:asyncio.run(model._get_learning_exclusion_summary('2026-10-11',language))),
                            outcome(lambda:asyncio.run(model._get_yesterday_shadow_summary(language))),
                            model._row_value({'key':None},'key',0),model._row_value([7],'key',0),
                            model._row_value([],'key',0)]
                    results.append((values,events))
                self.assertEqual(results[1],results[0])

    def test_full_renderer_date_fallback_weather_astronomy_and_dev_gates(self):
        for language in ('de','en'):
            for dev in (False,True):
                for variant in ('normal','minimal','invalid-date'):
                    results=[]
                    for side in (0,1):
                        events=[]
                        model=briefing(side,events,dev=dev)
                        async def peak(): events.append(('peak',)); return {'hour':12,'kwh':1.2,'method':'owned'}
                        async def status(language): events.append(('status',language)); return ['owned status']
                        async def shadow(language): events.append(('shadow',language)); return 'owned shadow'
                        model._get_peak_hour=peak; model._get_system_status=status; model._get_yesterday_shadow_summary=shadow
                        forecast={'date':'invalid' if variant=='invalid-date' else '2026-10-12','prediction_kwh':5}
                        yesterday=None if variant=='minimal' else {'actual_kwh':2}
                        weather=None if variant=='minimal' else {'clouds':20,'temperature':15.5}
                        astronomy=None if variant=='minimal' else {'daylight_hours':12.5,
                                   'sunrise':'2026-10-12T06:00:00','sunset':'2026-10-12 18:30:00'}
                        value=asyncio.run(model._generate_briefing_message(forecast,yesterday,astronomy,weather,language))
                        results.append((value,events))
                    self.assertEqual(results[1],results[0])
                    self.assertIn('Forecast: 5.00 kWh',results[1][0]['message'])

    def test_notification_payloads_service_checks_errors_and_cancellation(self):
        for language in ('de','en'):
            for scenario in ('normal','mobile','missing-service','no-forecast','render-error',
                             'persistent-error','mobile-error','cancelled'):
                results=[]
                for side in (0,1):
                    events=[]
                    class Services:
                        def has_service(self,domain,name):
                            events.append(('has-service',domain,name)); return scenario!='missing-service'
                        async def async_call(self,domain,name,payload,**options):
                            events.append(('service',domain,name,copy.deepcopy(payload),options))
                            if scenario=='cancelled': raise asyncio.CancelledError('owned cancellation')
                            if scenario=='persistent-error' and name=='persistent_notification':
                                raise RuntimeError('owned persistent failure')
                            if scenario=='mobile-error' and name=='mobile_app_owned':
                                raise RuntimeError('owned mobile failure')
                    model=briefing(side,events,services=Services())
                    async def forecast():
                        events.append(('forecast',)); return None if scenario=='no-forecast' else {'prediction_kwh':5}
                    async def yesterday(): events.append(('yesterday',)); return None
                    async def astronomy(): events.append(('astronomy',)); return None
                    async def weather(): events.append(('weather',)); return {'clouds':20,'temperature':15.5}
                    async def render(*arguments):
                        events.append(('render',copy.deepcopy(arguments)))
                        if scenario=='render-error': raise ValueError('owned renderer failure')
                        return {'title':'owned title','message':'owned full message'}
                    model._get_today_forecast_data=forecast; model._get_yesterday_actual_data=yesterday
                    model._get_astronomy_data=astronomy; model._get_today_weather_data=weather
                    model._generate_briefing_message=render
                    notify='notify.mobile_app_owned' if scenario in ('mobile','missing-service','mobile-error') else 'persistent_notification'
                    value=outcome(lambda:asyncio.run(model.send_daily_briefing(notify,language)))
                    results.append((value,events))
                self.assertEqual(results[1],results[0])


if __name__=='__main__':
    unittest.main()
