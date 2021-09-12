# -*- coding: utf-8 -*-
#########################################################
# imports
#########################################################
# python
import os, sys, traceback, re, json, threading, base64
from datetime import datetime, timedelta
import copy
# third-party
import requests
# third-party
from flask import request, render_template, jsonify
from sqlalchemy import or_, and_, func, not_, desc
# sjva 공용
from framework import app, db, scheduler, path_data, socketio
from framework.util import Util
from framework.common.celery import shutil_task 
from plugin import LogicModuleBase, default_route_socketio
# 패키지
from .plugin import P
logger = P.logger
ModelSetting = P.ModelSetting

#########################################################
# global utils
#########################################################

#########################################################
# main logic
#########################################################
'''
TODO:
audio_only 를 단순히 mp3로 저장하니까 재생이 안되는거같아
'''
class LogicTwitch(LogicModuleBase):
  db_default = {
    'twitch_db_version': '1',
    'twitch_download_path': os.path.join(path_data, P.package_name, 'twitch'),
    'twitch_filename_format': '[%Y-%m-%d %H:%M][{category}] {title} part{part_number}',
    'twitch_directory_name_format': '{author} ({streamer_id})/%Y-%m',
    'twitch_file_split_by_size': 'True',
    'twitch_file_size_limit': '2 GB',
    'twitch_streamer_ids': '',
    'twitch_auto_make_folder': 'True',
    'twitch_auto_start': 'False',
    'twitch_interval': '2',
    'streamlink_quality': '1080p60,best',
    'streamlink_twitch_disable_ads': 'True',
    'streamlink_twitch_disable_hosting': 'True',
    'streamlink_twitch_disable_reruns': 'True',
    'streamlink_twitch_low_latency': 'True',
    'streamlink_hls_live_edge': 1,
    'streamlink_chunk_size': '128',
    'streamlink_options': 'False', # html 토글 위한 쓰레기 값임.
  }
  is_streamlink_installed = False
  
  streamlink_plugins = {}
  '''
  'streamer_id': <StreamlinkTwitchPlugin> 
  '''
  download_status = {}
  '''
  'streamer_id': {
    'db_id': 0,
    'running': bool,
    'enable': bool,
    'online': bool,
    'author': str,
    'title': str,
    'category': str,
    'started_time': 0 or datetime object,
    'quality': '',
    'download_directory': '',
    'download_filenames': [],
    'filename_format': '',  
    'do_split': bool, 
    'size_limit': '',
    'current_part_number': 1,
    'size': 0,
    'elapsed_time': '',
    'speed': '',
    'streams': {},
  }
  '''
  streamlink_session = None


  def __init__(self, P):
    super(LogicTwitch, self).__init__(P, 'setting', scheduler_desc='twitch 라이브 다운로드')
    self.name = 'twitch'
    default_route_socketio(P, self)


  def process_menu(self, sub, req):
    arg = P.ModelSetting.to_dict()
    arg['sub'] = self.name
    if sub in ['setting', 'status', 'list']:
      if sub == 'setting':
        job_id = f'{self.P.package_name}_{self.name}'
        arg['scheduler'] = str(scheduler.is_include(job_id))
        arg['is_running'] = str(scheduler.is_running(job_id))
        arg['is_streamlink_installed'] = 'Installed' if self.is_streamlink_installed else 'Not installed'
      return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)
    return render_template('sample.html', title=f'404: {P.package_name} - {sub}')


  def process_ajax(self, sub, req):
    try:
      if sub == 'entity_list': # status 초기화
        return jsonify(self._get_download_status_for_javascript())
      elif sub == 'toggle':
        streamer_id = req.form['streamer_id']
        command = req.form['command']
        result = {
          'previous_status': 'offline',
        }
        if command == 'disable':
          result['previous_status'] = 'online' if self.download_status[streamer_id]['online'] else 'offline'
          self._set_download_status(streamer_id, {'enable': False})
        elif command == 'enable':
          self._set_download_status(streamer_id, {'enable': True})
        return jsonify(result)
      elif sub == 'install':
        LogicTwitch._install_streamlink()
        self.is_streamlink_installed = True
        return jsonify({})
      elif sub == 'web_list': # list 탭에서 요청
        database = ModelTwitchItem.web_list(req)
        database['streamer_ids'] = ModelTwitchItem.get_streamer_ids()
        return jsonify(database)
      elif sub == 'db_remove':
        db_id = req.form['id']
        is_running = len([
          i for i in self.download_status 
          if int(self.download_status[i]['db_id']) == int(db_id) and \
            self.download_status[i]['running']
        ]) > 0
        if is_running:
          return jsonify({'ret': False, 'msg': '다운로드 중인 항목입니다.'})
        
        delete_file = req.form['delete_file'] == 'true'
        if delete_file:
          download_info = ModelTwitchItem.get_file_list_by_id(db_id)
          for filename in download_info['filenames']:
            download_path = os.path.join(download_info['directory'], filename)
            shutil_task.remove(download_path)
        db_return = ModelTwitchItem.delete_by_id(db_id)
        return jsonify({'ret': db_return})
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
      return jsonify(({'ret': False, 'msg': e}))


  def setting_save_after(self):
    streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
    before_streamer_ids = [id for id in self.streamlink_plugins]
    old_streamer_ids = [id for id in before_streamer_ids if id not in streamer_ids]
    new_streamer_ids = [id for id in streamer_ids if id not in before_streamer_ids]
    for streamer_id in old_streamer_ids: 
      self._set_download_status(streamer_id, {'enable': False})
    for streamer_id in new_streamer_ids:
      self._clear_properties(streamer_id)
    self._set_streamlink_options()


  def scheduler_function(self):
    '''
    여기서는 다운로드 요청만 하고
    status 갱신은 실제 다운로드 로직에서 
    '''
    try:
      if self.streamlink_session is None:
        import streamlink
        self.streamlink_session = streamlink.Streamlink()
        self._set_streamlink_options()

      streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
      for streamer_id in streamer_ids:
        if not self.download_status[streamer_id]['enable']:
          continue
        if self.download_status[streamer_id]['running']:
          continue
        if self.streamlink_plugins[streamer_id] is None:
          url = 'https://www.twitch.tv/' + streamer_id
          self.streamlink_plugins[streamer_id] = self.streamlink_session.resolve_url(url)
        is_online = self._is_online(streamer_id)
        if not is_online:
          continue
        self._set_download_status(streamer_id, {'running': True})
        self._download(streamer_id)
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def plugin_load(self):
    try:
      import streamlink
      self.is_streamlink_installed = True
      self.streamlink_session = streamlink.Streamlink()
      self._set_streamlink_options()
    except:
      return False
    if not os.path.isdir(P.ModelSetting.get('twitch_download_path')):
      os.makedirs(P.ModelSetting.get('twitch_download_path'), exist_ok=True) # mkdir -p
    ModelTwitchItem.plugin_load()
    streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
    for streamer_id in streamer_ids:
      self._clear_properties(streamer_id)


  def reset_db(self):
    db.session.query(ModelTwitchItem).delete()
    db.session.commit()
    return True


  #########################################################

  # imported from soju6jan/klive/logic_streamlink.py
  @staticmethod
  def _install_streamlink():
    try:
      def func():
        import system
        import framework.common.util as CommonUtil
        commands = [['msg', u'잠시만 기다려주세요.']]
        if CommonUtil.is_docker():
          commands.append(['apk', 'add', '--no-cache', '--virtual', '.build-deps', 'gcc', 'g++', 'make', 'libffi-dev', 'openssl-dev'])

        commands.append([app.config['config']['pip'], 'install', '--upgrade', 'pip'])
        commands.append([app.config['config']['pip'], 'install', '--upgrade', 'setuptools'])
        commands.append([app.config['config']['pip'], 'install', 'streamlink'])
        if CommonUtil.is_docker():
          commands.append(['apk', 'del', '.build-deps'])
        commands.append(['msg', u'설치가 완료되었습니다.'])
        system.SystemLogicCommand.start('설치', commands)
      t = threading.Thread(target=func, args=())
      t.setDaemon(True)
      t.start()
    except Exception as e:
      logger.error('Exception:%s', e)
      logger.error(traceback.format_exc())


  def _is_online(self, streamer_id):
    return len(self.streamlink_plugins[streamer_id].streams()) > 0


  def _get_title(self, streamer_id):
    return self.streamlink_plugins[streamer_id].get_title()


  def _get_author(self, streamer_id):
    return self.streamlink_plugins[streamer_id].get_author()


  def _get_category(self, streamer_id):
    return self.streamlink_plugins[streamer_id].get_category()


  def _get_options(self):
    '''
    from P.Modelsetting produces list for options list
    '''
    options = []
    streamlink_twitch_disable_ads = P.ModelSetting.get_bool('streamlink_twitch_disable_ads')
    streamlink_twitch_disable_hosting = P.ModelSetting.get_bool('streamlink_twitch_disable_hosting')
    streamlink_twitch_disable_reruns = P.ModelSetting.get_bool('streamlink_twitch_disable_reruns')
    streamlink_twitch_low_latency = P.ModelSetting.get_bool('streamlink_twitch_low_latency')
    streamlink_hls_live_edge = P.ModelSetting.get_int('streamlink_hls_live_edge')
    options = options + [
      ('twitch', 'disable-ads', streamlink_twitch_disable_ads),
      ('twitch', 'disable-hosting', streamlink_twitch_disable_hosting),
      ('twitch', 'disable-reruns', streamlink_twitch_disable_reruns),
      ('twitch', 'low-latency', streamlink_twitch_low_latency),
      ('hls-live-edge', streamlink_hls_live_edge),
    ]
    return options


  def _get_options_string(self):
    result = ''
    options = self._get_options()
    for tup in options:
      if len(tup) == 2:
        result += tup[0] + ' ' + str(tup[1]) + '\n'
      else:
        result += tup[0] + ' ' + tup[1] + ' ' + str(tup[2]) + '\n'
    return result


  def _set_streamlink_options(self):
    options = self._get_options()
    for option in options:
      if len(option) == 2:
        self.streamlink_session.set_option(option[0], option[1])
      elif len(option) == 3:
        self.streamlink_session.set_plugin_option(option[0], option[1], option[2])


  def _set_download_directory(self, streamer_id):
    ''' 
    make download_directory and
    set 'download_directory'
    '''
    download_base_directory = P.ModelSetting.get('twitch_download_path')
    download_make_directory = P.ModelSetting.get_bool('twitch_auto_make_folder')
    download_directory_format = P.ModelSetting.get('twitch_directory_name_format')
    download_directory_string = ''
    if download_make_directory:
      download_directory_string = '/'.join([
        self._replace_unavailable_characters_in_filename(self._parse_string_from_format(streamer_id, directory_format) )
        for directory_format in download_directory_format.split('/')
      ])
    download_directory = os.path.join(download_base_directory, download_directory_string)
    if not os.path.isdir(download_directory):
      os.makedirs(download_directory, exist_ok=True)
    self._set_download_status(streamer_id, {'download_directory': download_directory})


  def _download(self, streamer_id):
    '''
    scheduler_function 에서
    1. session 옵션은 다 처리할 것임.
    2. resolve_url(url) 결과를 self.streamlink_plugins 에 저장
    status에는 options를 굳이 저장할 필요는 없음. db에는 필요할 지도?

    size_limit, chunk_size, quality 값은 있어야 함.
    '''
    quality = ''
    filename_format = ''

    download_filename_format = P.ModelSetting.get('twitch_filename_format')
    quality_options = [i.strip() for i in P.ModelSetting.get('streamlink_quality').split(',')]
    do_split = P.ModelSetting.get_bool('twitch_file_split_by_size')
    size_limit = P.ModelSetting.get('twitch_file_size_limit')
    chunk_size = P.ModelSetting.get_int('streamlink_chunk_size')
    streams = self.streamlink_plugins[streamer_id].streams()

    init_values = {
      'online': len(streams) > 0,
      'author': self.streamlink_plugins[streamer_id].get_author(),
      'title': self.streamlink_plugins[streamer_id].get_title(),
      'category': self.streamlink_plugins[streamer_id].get_category(),
      'streams': {q:streams[q].url for q in streams},
      'started_time': datetime.now(),
      'do_split': do_split,
      'size_limit': size_limit,
      'chunk_size': chunk_size,
      'options': self._get_options(),
    }
    self._set_download_status(streamer_id, init_values)
    
    self._set_download_directory(streamer_id)
    filename_format = self._parse_string_from_format(streamer_id, download_filename_format)

    for selected_quality in quality_options:
      if selected_quality in self.download_status[streamer_id]['streams']:
        quality = selected_quality
        break

    db_id = ModelTwitchItem.append(streamer_id, self.download_status[streamer_id])
    ModelTwitchItem.set_option_value(db_id, self._get_options_string())

    size_limit = self._byte_from_unit(size_limit)
    init_values2 = {
      'db_id': db_id,
      'filename_format': filename_format,
      'quality': quality,
    }
    self._set_download_status(streamer_id, init_values2)

    t = threading.Thread(target=self._download_thread_function, args=(streamer_id, ))
    t.setDaemon(True)
    t.start()


  def _download_thread_function(self, streamer_id):
    from time import time
    downloaded_bytes = 0

    download_directory = self.download_status[streamer_id]['download_directory']
    quality = self.download_status[streamer_id]['quality']
    do_split = self.download_status[streamer_id]['do_split']
    size_limit = self.download_status[streamer_id]['size_limit']
    chunk_size = self.download_status[streamer_id]['chunk_size']

    size_limit = self._byte_from_unit(size_limit)

    streams = self.streamlink_plugins[streamer_id].streams()
    stream = streams[quality]
    if quality in ['best', 'worst']: # convert best -> 1080p60, worst -> 160p
      quality = [
        _quality for _quality in streams
        if streams[quality] == streams[_quality] and \
          quality != _quality
      ][0]
      self._set_download_status(streamer_id, {'quality': quality})

    opened_stream = stream.open()

    started_time = time()
    before_time_for_status = time()
    current_time_for_status = time()
    before_bytes_for_status = 0
    current_bytes_for_status = 0

    filepath = ''

    if do_split:
      stop_flag = False
      while True and (not stop_flag):
        filepath = self._get_filepath(streamer_id)
        with open(filepath, 'wb') as target:
          ModelTwitchItem.update(self.download_status[streamer_id])
          logger.debug(f'Write file: {filepath}')
          current_part_number = self.download_status[streamer_id]['current_part_number']
          while True:
            if streamer_id not in self.streamlink_plugins: # streamer_ids 에서 삭제 되었을 경우
              stop_flag = True
              break
            if not self.download_status[streamer_id]['enable']: # 수동 정지
              stop_flag = True
              break
            try:
              target.write(opened_stream.read(chunk_size))
              downloaded_bytes += chunk_size
            except Exception as e: # opened_stream.closed 로 판별이 안됨. 
              logger.error(f'{e}')
              logger.debug(f'streamlink cannot read chunk OR sjva cannot write birnay file')
              stop_flag = True
              break
            current_time_for_status = time()
            current_bytes_for_status = downloaded_bytes
            if current_time_for_status - before_time_for_status > 3:
              time_diff = current_time_for_status - before_time_for_status
              byte_diff = current_bytes_for_status - before_bytes_for_status
              speed = self._get_speed_from_time(time_diff, byte_diff)
              self._set_download_status(streamer_id, {
                'size': downloaded_bytes,
                'elapsed_time': self._get_timestr_from_seconds(time() - started_time),
                'speed': speed,
              })
              ModelTwitchItem.update(self.download_status[streamer_id])
              before_time_for_status = current_time_for_status
              before_bytes_for_status = current_bytes_for_status
            if downloaded_bytes > (current_part_number * size_limit):
              break
          target.close()
    else:
      filepath = self._get_filepath(streamer_id)
      with open(filepath, 'wb') as target:
        ModelTwitchItem.update(self.download_status[streamer_id])
        logger.debug(f'Write file: {filepath}')
        while True:
          if streamer_id not in self.streamlink_plugins:
            break
          if not self.download_status[streamer_id]['enable']:
            break
          try:
            target.write(opened_stream.read(chunk_size))
            downloaded_bytes += chunk_size
          except Exception as e:
            logger.error(f'{e}')
            logger.debug(f'streamlink cannot read chunk OR sjva cannot write birnay file')
            break
          current_time_for_status = time()
          current_bytes_for_status = downloaded_bytes
          if current_time_for_status - before_time_for_status > 3:
            time_diff = current_time_for_status - before_time_for_status
            byte_diff = current_bytes_for_status - before_bytes_for_status
            speed = self._get_speed_from_time(time_diff, byte_diff)
            self._set_download_status(streamer_id, {
              'size': downloaded_bytes,
              'elapsed_time': self._get_timestr_from_seconds(time() - started_time),
              'speed': speed,
            })
            ModelTwitchItem.update(self.download_status[streamer_id])
            before_time_for_status = current_time_for_status
            before_bytes_for_status = current_bytes_for_status
        target.close()
   
    opened_stream.close()
    ModelTwitchItem.process_done(self.download_status[streamer_id])
    if os.path.exists(filepath) and os.path.getsize(filepath) < 512 * 1024: # delete last empty file when cancelled
      shutil_task.remove(filepath)
      self._set_download_status(streamer_id, {'download_filenames': self.download_status[streamer_id]['download_filenames'][:-1]})
      if len(self.download_status[streamer_id]['download_filenames']) == 0:
        ModelTwitchItem.delete_by_id(self.download_status[streamer_id]['db_id'])
    self._clear_properties(streamer_id)
    logger.debug(f'{streamer_id} stream ends.')
    if streamer_id not in [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]:
      # streamer_ids 업데이트 되어서 삭제 해야할 때
      del self.streamlink_plugins[streamer_id]
      del self.download_status[streamer_id]
  # _download_thread_function ends



  def _get_speed_from_time(self, time_diff, byte_diff):
    return self._unit_from_byte(byte_diff/time_diff) + '/s'


  def _get_timestr_from_seconds(self, seconds):
    result = ''
    hours = seconds // (60*60)
    seconds = seconds % (60*60)
    minutes = seconds // 60
    seconds = seconds % 60
    if hours:
      result += f'{int(hours)}h '
    if minutes:
      result += f'{int(minutes)}m '
    result += f'{int(seconds)}s'
    return result


  def _get_filename(self, streamer_id):
    '''
    update current_part_number, 'download_filenames'
    returns next_{filename}.mp4 
    '''
    do_split = self.download_status[streamer_id]['do_split']
    filename = self.download_status[streamer_id]['filename_format']
    is_audio = self.download_status[streamer_id]['quality'] == 'audio_only'
    next_part_number = self.download_status[streamer_id]['current_part_number'] + 1
    if do_split:
      self._set_download_status(streamer_id, {'current_part_number': next_part_number})
      filename = filename.replace('{part_number}', str(next_part_number))
    else:
      filename = filename.replace('{part_number}', '')
    filename = self._replace_unavailable_characters_in_filename(filename)
    if is_audio:
      filename = filename + '.mp3'
    else:
      filename = filename + '.mp4'
    self._set_download_status(streamer_id, {'download_filenames': self.download_status[streamer_id]['download_filenames'] + [filename]})
    return filename


  def _get_filepath(self, streamer_id):
    '''
    raise exception if path already exists

    returns {download_directory} + {filename}  
    '''
    download_directory = self.download_status[streamer_id]['download_directory']
    filename = self._get_filename(streamer_id)
    filepath = os.path.join(download_directory, filename)
    if os.path.exists(filepath):
      self._clear_properties(streamer_id)
      raise Exception(f'[{streamer_id}] Failed! {filepath} already exists!')
    return filepath


  def _unit_from_byte(self, byte: int or float):
    '''
    returns '23 MB'
    '''
    count = 0
    byte = float(byte)
    while byte > 1024:
      byte = byte/1024
      count += 1
    byte = round(byte, 1)
    byte = str(byte)
    if count == 0:
      byte += ' B'
    elif count == 1:
      byte += ' KB'
    elif count == 2:
      byte += ' MB'
    elif count == 3:
      byte += ' GB'
    elif count == 4:
      byte += ' TB'
    return byte


  def _byte_from_unit(self, units: str):
    '''
    units: '2.8 MB', '4.0KB', ...
    '''
    value = float(re.findall(r"\d*\.\d+|\d+", units)[0])
    if 'T' in units:
      value = value * 1024 * 1024 * 1024 * 1024
    elif 'G' in units:
      value = value * 1024 * 1024 * 1024
    elif 'M' in units:
      value = value * 1024 * 1024
    elif 'K' in units:
      value = value * 1024
    return value


  def _replace_unavailable_characters_in_filename(self, source):
    replace_list = {
      ':': '∶',
      '/': '-',
      '\\': '-',
      '*': '⁎',
      '?': '？',
      '"': "'",
      '<': '(',
      '>': ')',
      '|': '_',
    }
    for key in replace_list.keys():
      source = source.replace(key, replace_list[key])
    return source


  def _parse_string_from_format(self, streamer_id, format_str):
    '''
    keywords: {author}, {title}, {category}, {streamer_id}
    and time foramt keywords: %m,%d,%Y, %H,%M,%S, ...
    https://docs.python.org/ko/3/library/datetime.html#strftime-and-strptime-format-codes
    '''
    result = format_str
    result = result.replace('{streamer_id}', streamer_id)
    result = result.replace('{author}', self.download_status[streamer_id]['author'])
    result = result.replace('{title}', self.download_status[streamer_id]['title'])
    result = result.replace('{category}', self.download_status[streamer_id]['category'])
    result = datetime.now().strftime(result)
    return result


  def _set_download_status(self, streamer_id, values: dict):
    '''
    set download_status and 
    send socketio_callback('status')
    '''
    if streamer_id not in self.download_status:
      self.download_status[streamer_id] = {}
    for key in values:
      self.download_status[streamer_id][key] = values[key]
    self.socketio_callback('update', self._get_download_status_for_javascript(streamer_id))


  def _get_download_status_for_javascript(self, streamer_id=None):
    '''
    if streamer_id specified, it returns one object

    returns time-converted status 
    '''
    if streamer_id is not None:
      import copy
      status_streamer_id = copy.deepcopy(self.download_status[streamer_id])
      started_time = status_streamer_id['started_time']
      if started_time != 0:
        status_streamer_id['started_time'] = started_time.strftime('%Y-%m-%d %H:%M')
      return {'streamer_id': streamer_id, 'status': status_streamer_id}
    else:
      return {
        id:self._get_download_status_for_javascript(id)['status'] for id in self.download_status
      }

  
  def _clear_properties(self, streamer_id):
    if streamer_id in self.streamlink_plugins:
      del self.streamlink_plugins[streamer_id]
    self.streamlink_plugins[streamer_id] = None
    self._clear_download_status(streamer_id)
    

  def _clear_download_status(self, streamer_id):
    enable_value = True
    if streamer_id in self.download_status:
      enable_value = self.download_status[streamer_id]['enable']
    default_values = {
      'db_id': -1,
      'running': False,
      'enable': enable_value,
      'online': False,
      'author': 'No Author',
      'title': 'No Title',
      'category': 'No Category',
      'started_time': 0,
      'quality': 'No Quality',
      'download_directory': '',
      'download_filenames': [],
      'filename_format': '',
      'do_split': P.ModelSetting.get_bool('twitch_file_split_by_size'),
      'size_limit': P.ModelSetting.get('twitch_file_size_limit'),
      'current_part_number': 0,
      'size': 0,
      'elapsed_time': 'No time',
      'speed': 'No Speed',
      'streams': {},
      'options': [],
    }
    self._set_download_status(streamer_id, default_values)


#########################################################
# db
#########################################################
class ModelTwitchItem(db.Model):
  '''
  파일 저장에 관한 정보
  created_time(날짜, 시간),
  streamer_id, author,
  title(started), category(started),
  download_path, 
  file_size, # 실시간 업데이트
  elapsed_time, # 실시간 업데이트
  quality, options
  '''
  __tablename__ = '{package_name}_twitch_item'.format(package_name=P.package_name)
  __table_args__ = {'mysql_collate': 'utf8_general_ci'}
  __bind_key__ = P.package_name
  id = db.Column(db.Integer, primary_key=True)
  created_time = db.Column(db.DateTime)
  running = db.Column(db.Boolean)
  streamer_id = db.Column(db.String)
  author = db.Column(db.String)
  title = db.Column(db.String)
  category = db.Column(db.String)
  download_directory = db.Column(db.String)
  download_filenames = db.Column(db.String)
  file_size = db.Column(db.BigInteger)
  elapsed_time = db.Column(db.String)
  quality = db.Column(db.String)
  options = db.Column(db.String)


  def __init__(self):
    self.running = True

  def __repr__(self):
    return repr(self.as_dict())

  def as_dict(self):
    ret = {x.name: getattr(self, x.name) for x in self.__table__.columns}
    ret['created_time'] = self.created_time.strftime('%Y-%m-%d %H:%M')
    return ret

  def save(self):
    db.session.add(self)
    db.session.commit()

  @classmethod
  def get_by_id(cls, id):
    return db.session.query(cls).filter_by(id=id).first()

  @classmethod
  def delete_by_id(cls, id):
    db.session.query(cls).filter_by(id=id).delete()
    db.session.commit()
    return True
  
  @classmethod
  def get_file_list_by_id(cls, id):
    item = cls.get_by_id(id)
    filenames = item.download_filenames.split('\n')
    return {
      "directory": item.download_directory,
      "filenames": filenames,
    }

  @classmethod
  def web_list(cls, req):
    ret = {}
    page = int(req.form['page']) if 'page' in req.form else 1
    page_size = 30
    job_id = ''
    search = req.form['search_word'] if 'search_word' in req.form else ''
    option = req.form['option'] if 'option' in req.form else 'all'
    order = req.form['order'] if 'order' in req.form else 'desc'
    query = cls.make_query(search=search, order=order, option=option)
    count = query.count()
    query = query.limit(page_size).offset((page-1)*page_size)
    lists = query.all()
    ret['list'] = [item.as_dict() for item in lists]
    ret['paging'] = Util.get_paging_info(count, page, page_size)
    return ret

  @classmethod
  def make_query(cls, search='', order='desc', option='all'):
    query = db.session.query(cls)
    conditions = []

    if search is not None and search != '':
      if search.find('|') != -1:
        tmp = search.split('|')
        for tt in tmp:
          if tt != '':
            conditions.append(cls.title.like('%'+tt.strip()+'%') )
            conditions.append(cls.author.like('%'+tt.strip()+'%') )
            conditions.append(cls.category.like('%'+tt.strip()+'%') )
      elif search.find(',') != -1:
        tmp = search.split(',')
        for tt in tmp:
          if tt != '':
            conditions.append(cls.title.like('%'+tt.strip()+'%') )
            conditions.append(cls.author.like('%'+tt.strip()+'%') )
            conditions.append(cls.category.like('%'+tt.strip()+'%') )
      else:
        conditions.append(cls.title.like('%'+search+'%') )
        conditions.append(cls.author.like('%'+search+'%') )
        conditions.append(cls.category.like('%'+search+'%') )
      query = query.filter(or_(*conditions))
    
    if option != 'all':
      query = query.filter(cls.streamer_id == option)

    query = query.order_by(desc(cls.id)) if order == 'desc' else query.order_by(cls.id)
    return query


  @classmethod
  def plugin_load(cls):
    items = db.session.query(cls).filter(cls.file_size < 524288).all() # 512 * 1024
    for item in items:
      file_list = cls.get_file_list_by_id(item.id)
      directory = file_list['directory']
      filenames = file_list['filenames']
      for filename in filenames:
        filepath = os.path.join(directory, filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
          shutil_task.remove(filepath)
      cls.delete_by_id(item.id)
    db.session.query(cls).update({'running': False})
    db.session.commit()
  
  @classmethod
  def process_done(cls, single_download_status):
    cls.update(single_download_status)
    item = cls.get_by_id(single_download_status['db_id'])
    item.running = False
    item.save()


  @classmethod
  def delete_empty_items(cls):
    db.session.query(cls).filter_by(file_size="No Size").delete()
    db.session.commit()
    return True
  

  @classmethod
  def get_streamer_ids(cls):
    return [item.streamer_id for item in db.session.query(cls.streamer_id).distinct()]


  @classmethod
  def append(cls, streamer_id, single_download_status):
    item = ModelTwitchItem()
    item.created_time = single_download_status['started_time']
    item.streamer_id = streamer_id
    item.author = single_download_status['author']
    item.title = single_download_status['title']
    item.category = single_download_status['category']
    item.download_directory = single_download_status['download_directory']
    item.download_filenames = '\n'.join(single_download_status['download_filenames'])
    item.file_size = single_download_status['size']
    item.elapsed_time = single_download_status['elapsed_time']
    item.quality = single_download_status['quality']
    item.save()
    return item.id

  @classmethod
  def update(cls, single_download_status):
    item = cls.get_by_id(single_download_status['db_id'])
    item.download_filenames = '\n'.join(single_download_status['download_filenames'])
    item.quality = single_download_status['quality']
    item.file_size = single_download_status['size']
    item.elapsed_time = single_download_status['elapsed_time']
    item.save()
  
  @classmethod
  def set_option_value(cls, db_id, options):
    item = cls.get_by_id(db_id)
    item.options = options
    item.save()
