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
from plugin import LogicModuleBase, default_route_socketio
# 패키지
from .plugin import P
logger = P.logger
ModelSetting = P.ModelSetting

#########################################################
# utils
#########################################################


#########################################################
# main logic
#########################################################
class LogicTwitch(LogicModuleBase):
  db_default = {
    'twitch_db_version': '1',
    'twitch_download_path': os.path.join(path_data, P.package_name, 'twitch'),
    'twitch_filename_format': '[%Y-%m-%d %H:%M][{category}] {title}',
    'twitch_directory_name_format': '{author} ({streamer_id})/%Y-%m',
    'twitch_streamer_ids': '',
    'twitch_auto_make_folder': 'True',
    'twitch_auto_start': 'False',
    'twitch_interval': '1',
    'streamlink_quality': '1080p60,best',
    'streamlink_options': '--twitch-disable-hosting\n--twitch-disable-ads\n--twitch-disable-reruns\n',
  }
  twitch_prefix = 'https://www.twitch.tv/'
  is_streamlink_installed = False
  streamlink_plugins = {}
  '''
  'streamer_id': <Streamlink.plugin>
  '''
  streamlink_processes = {}
  '''
  'streamer_id': <subprocess.Popen>
  '''
  streamlink_process_status = {}
  '''
  'streamer_id': {
    'db_id': 0,
    'enable': bool,
    'online': bool,
    'author': str,
    'title': str,
    'category': str,
    'started_time': 0 or datetime object,
    'quality': '',
    'options': [],
    'download_path': '',
    'logs': ['message1', 'message2',],
    'status': 'status_string',
    'size': '',
    'elapsed_time': '',
    'speed': '',
    'streams': [],
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
        return jsonify(self.__get_converted_streamlink_process_status())
      elif sub == 'toggle':
        streamer_id = req.form['streamer_id']
        command = req.form['command']
        result = {
          'previous_status': 'offline',
        }

        if command == 'disable':
          '''
          process 종료하면서 데몬으로 __set_default_streamlink_process_status처리하는데
          그게 여기서 'enable': False 하는 것보다 나중에 실행되서
          반영이 안됨.

          그래서 __set_default_streamlink_process_status 함수에서
          이전 상태 참조하는 코드 작성함
          '''
          if self.streamlink_processes[streamer_id]:
            self.streamlink_processes[streamer_id].terminate()
            self.streamlink_processes[streamer_id].wait()
            result['previous_status'] = 'online'
          self.__set_streamlink_process_status(streamer_id, 'enable', False)
        elif command == 'enable':
          self.__set_streamlink_process_status(streamer_id, 'enable', True)

        return jsonify(result)
      elif sub == 'install':
        LogicTwitch.install_streamlink()
        self.is_streamlink_installed = True
        return jsonify({})
      elif sub == 'web_list': # list 탭에서 요청
        database = ModelTwitchItem.web_list(req)
        database['streamer_ids'] = ModelTwitchItem.get_streamer_ids()
        return jsonify(database)
      elif sub == 'db_remove':
        db_id = req.form['id']
        is_running_process = [
          i for i in self.streamlink_process_status 
          if int(self.streamlink_process_status[i]['db_id']) == int(db_id)
        ]
        if len(is_running_process) > 0:
          # 실행중인 프로세스면 안됨.
          # 어차피 interval 뒤에 다시 다운될건데?
          return jsonify({'ret': False, 'msg': '다운로드 중인 항목입니다.'})
        delete_file = req.form['delete_file'] == 'true'
        if delete_file:
          download_path = ModelTwitchItem.get_file_by_id(db_id)
          from framework.common.celery import shutil_task 
          result = shutil_task.remove(download_path)
        db_return = ModelTwitchItem.delete_by_id(db_id)
        return jsonify({'ret': db_return})
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
      return jsonify(({'ret': False, 'msg': e}))


  def setting_save_after(self):
    '''
    아이디 추가하면 스케줄링 한번 돌리려고 했는데
    그러면 설정 저장에서 로딩생기네 
    '''
    streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
    before_streamer_ids = [id for id in self.streamlink_plugins]
    old_streamer_ids = [id for id in before_streamer_ids if id not in streamer_ids]
    new_streamer_ids = [id for id in streamer_ids if id not in before_streamer_ids]
    for old_streamer_id in old_streamer_ids:
      proc = self.streamlink_processes[old_streamer_id]
      if proc is not None:
        proc.terminate()
        proc.wait()
        ModelTwitchItem.process_done(self.streamlink_process_status[old_streamer_id])
        logger.debug(proc.returncode)
      del self.streamlink_processes[old_streamer_id]
      del self.streamlink_plugins[old_streamer_id]
      del self.streamlink_process_status[old_streamer_id]
    for new_streamer_id in new_streamer_ids:
      self.__init_properties(new_streamer_id)


  # TODO: 
  # 임시 디렉토리 설정 할까 말까
  # rclone 마운트에 직접 다운했을 때 문제 생기면 추가하자
  #
  # TODO:
  # self.streamlink_plugins 이 제대로 갱신이 안되는건지
  # 정지 이후에 다시 다운로드를 했는데
  # title이 outdated된 값으로 설정되었음.
  # 다운로드에는 큰 문제가 아니니 일단 스킵.
  #
  # TODO:
  # 스트림 시작 매우 초기에 1080p60 (source) 이 있을텐데도,
  # best로 설정했는데 720p60 으로 다운로드 되는 문제
  # -> 일단 화질을 1080p60,best로 설정해놓음
  # 이래도 안되면 트위치 초기 문제일 것
  #
  def scheduler_function(self):
    try:
      if not self.streamlink_session:
        import streamlink
        self.streamlink_session = streamlink.Streamlink()

      streamlink_quality = P.ModelSetting.get('streamlink_quality')
      streamlink_options = P.ModelSetting.get_list('streamlink_options', '|')
      streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]

      download_directory = P.ModelSetting.get('twitch_download_path')
      make_child_directory = P.ModelSetting.get_bool('twitch_auto_make_folder')
      child_directory_name_format = P.ModelSetting.get('twitch_directory_name_format')
      filename_format = P.ModelSetting.get('twitch_filename_format')


      self.streamlink_plugins = {
        streamer_id: self.streamlink_session.resolve_url(self.twitch_prefix + streamer_id) for streamer_id in streamer_ids
        if self.streamlink_process_status[streamer_id]['enable'] # status 에서 enable인 상태
      }

      for streamer_id in self.streamlink_plugins:
        # 진행중인 프로세스가 있는지 체크
        # set_info 뒤로 빼면 실시간으로 제목과 카테고리가 갱신될 것으로 생각함
        # 근데 그거는 트위치 서버에 부담될 것 같고, 이미 첫 정보로 파일 저장했으니
        # 딱히 필요 없을 거라고 생각함
        # 동영상 파일에 챕터 추가하는거면 좋겠는데 그거는 서버 사양 많이 탈 듯
        if self.streamlink_processes[streamer_id]:
          continue

        self.__set_default_streamlink_process_status(streamer_id)
        self.__set_streamlink_info(streamer_id)

        if self.streamlink_process_status[streamer_id]['online']:
          # 여기서 다운로드 시작
          # set filename
          download_path = os.path.abspath(download_directory)
          if make_child_directory:
            directory_name = self.__parse_title_string(streamer_id, child_directory_name_format)
            directory_name = '/'.join([self.__replace_unavailable_characters(folder) for folder in directory_name.split('/')])
            download_path = os.path.join(download_path, directory_name)
            if not os.path.isdir(download_path):
              os.makedirs(download_path, exist_ok=True) # mkdir -p
          filename = self.__parse_title_string(streamer_id, filename_format)
          filename = self.__replace_unavailable_characters(filename)
          download_path = os.path.join(download_path, filename)
          if not download_path.endswith('.mp4'):
            download_path = download_path + '.mp4'
          self.__set_streamlink_process_status(
            streamer_id,
            ['download_path', 'started_time', 'options', 'quality'],
            [download_path, datetime.now(), streamlink_options, streamlink_quality]
          )
          # db에 추가
          db_id = ModelTwitchItem.append(streamer_id, self.streamlink_process_status)
          self.__set_streamlink_process_status(streamer_id, 'db_id', db_id)
          # spawn child
          logger.debug(f'[download][{streamer_id}][{self.streamlink_process_status[streamer_id]["category"]}] {self.streamlink_process_status[streamer_id]["title"]}')
          self.__spawn_children(
            streamer_id,
            download_path,
            quality=streamlink_quality,
            options=streamlink_options
          )
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def plugin_load(self):
    try:
      import streamlink
      self.is_streamlink_installed = True
    except:
      return False
    if not os.path.isdir(P.ModelSetting.get('twitch_download_path')):
      os.makedirs(P.ModelSetting.get('twitch_download_path'), exist_ok=True) # mkdir -p
    ModelTwitchItem.plugin_load()
    streamer_ids = [id for id in P.ModelSetting.get_list('twitch_streamer_ids', '|') if not id.startswith('#')]
    for streamer_id in streamer_ids:
      self.__init_properties(streamer_id)



  def reset_db(self):
    db.session.query(ModelTwitchItem).delete()
    db.session.commit()
    return True


  #########################################################

  # imported from soju6jan/klive/logic_streamlink.py
  @staticmethod
  def install_streamlink():
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


  def __get_converted_streamlink_process_status_streamer_id(self, streamer_id):
    import copy
    status_streamer_id = copy.deepcopy(self.streamlink_process_status[streamer_id])
    started_time = status_streamer_id['started_time']
    if started_time != 0:
      status_streamer_id['started_time'] = started_time.strftime('%Y-%m-%d %H:%M')
    return {'streamer_id': streamer_id, 'status': status_streamer_id}
  

  def __get_converted_streamlink_process_status(self):
    import copy
    status = {}
    for streamer_id in self.streamlink_process_status:
      status[streamer_id] = self.__get_converted_streamlink_process_status_streamer_id(streamer_id)['status']
    return status


  def __set_streamlink_process_status(self, streamer_id, key, value):
    '''
    set streamlink_process_status and send socketio_callback('status')
    key and value can be string or list
    '''
    if type(key) == list:
      for i in range(len(key)):
        self.streamlink_process_status[streamer_id][key[i]] = value[i]
    else:
      self.streamlink_process_status[streamer_id][key] = value
    # 모든 status 보내면 비효율적이니까 하나씩 보냄
    self.socketio_callback('update', self.__get_converted_streamlink_process_status_streamer_id(streamer_id))


  def __clear_process_after_done(self, streamer_id):
    self.streamlink_plugins[streamer_id] = None
    self.streamlink_processes[streamer_id] = None
    self.__set_default_streamlink_process_status(streamer_id)
    logger.debug(f'[completed][{streamer_id}]')


  def __set_default_streamlink_process_status(self, streamer_id: str):
    enable_value = True
    if streamer_id in self.streamlink_process_status and \
      'enable' in self.streamlink_process_status[streamer_id]:
      enable_value = self.streamlink_process_status[streamer_id]['enable']
    keys = [
      'db_id',
      'enable', 'online', 'author',
      'title', 'category', 'started_time',
      'quality', 'options', 'download_path',
      'logs', 'status', 'size',
      'elapsed_time', 'speed', 'streams'
    ]
    values = [
      -1,
      enable_value, False, 'No Author',
      'No Title', 'No Category', 0,
      'No Quality', [], 'No Path',
      [], 'No Status', 'No Size',
      'No Time', 'No Speed', {}
    ]
    self.__set_streamlink_process_status(
      streamer_id,
      keys,
      values
    )


  def __spawn_children(self, streamer_id, filename, quality='best', options: list=[]):
    try:
      # 지금 output_reader에서는 현재 진행상황이 표시가 안되네
      # progress 표시하는게 버퍼에 쌓이지 않아서 그럼
      # -> bufsize=0, universal_newlines=True으로 해결
      # [download][filename]에서 truncated되는건 streamlink자체 문제임. 사이즈 조절로 해결 안됨.
      command = ['python3', '-m', 'streamlink', '--force-progress', '--output', filename]
      command = command + options
      command = command + [f'{self.twitch_prefix}{streamer_id}', quality]

      from subprocess import Popen, PIPE, STDOUT
      self.streamlink_processes[streamer_id] = Popen(
        command,
        stdout=PIPE,
        stderr=STDOUT,
        universal_newlines=True,
        bufsize=0,
      )
      t = threading.Thread(target=self.__streamlink_process_handler, args=(self.streamlink_processes[streamer_id], streamer_id,))
      t.setDaemon(True)
      t.start()
      return f'spawn success: {streamer_id}'
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
      return f'spawn failed: {streamer_id}'


  def __parse_download_log(self, log):
    info = {}
    raw = log.split(']')[-1].strip()
    size_raw, time_speed_raw = raw.split('(')
    size = size_raw.split('Written')[-1].strip()
    elapsed_time, speed = time_speed_raw.split('@')
    elapsed_time = elapsed_time.split('(')[-1].strip() # 아마도 '(' 이거 필요 없을 걸
    speed = speed.split(')')[0].strip()

    info['size'] = size
    info['elapsed_time'] = elapsed_time
    info['speed'] = speed
    return info


  def __streamlink_process_handler(self, process, streamer_id):
    ''' 
    1. 프로세스 로그 감시
    2. 프로세스 종료하면 ModelTwitchItem.running = False로 변경
    3. 프로세스 종료하면 self.__clear_process_after_done 실행
    
    TODO:
    수동으로 정지했을 때는 정상적으로 꺼짐.
    다운로드 완료했을 때 정상적으로 정지하는지 확인 안해봤음.
    '''
    for line in iter(process.stdout.readline, b''):
      line = str(line).strip()
      if len(line) < 1:
        continue

      # [info] [warning] 체크는 왜 필요하냐: 프로세스 종료될 때 마지막 코드가 한 라인에 나옴
      if line.startswith('[download]') and '[info]' not in line and '[warning]' not in line:
        info = self.__parse_download_log(line)
        keys = [i for i in info]
        keys.append('status')
        values = [info[i] for i in info]
        values.append(line)
        self.__set_streamlink_process_status(
          streamer_id,
          keys,
          values
        )
        ModelTwitchItem.update(self.streamlink_process_status[streamer_id])
      else:
        keys = ['logs']
        values = [self.streamlink_process_status[streamer_id]['logs']+[line]]
        if 'Opening stream:' in line:
          quality = line.split()[-2]
          keys.append('quality')
          values.append(quality)
        self.__set_streamlink_process_status(
          streamer_id,
          keys,
          values
        )
      # 정상적으로 stream 끝나면 [cli][info] Closing currently open stream... 나옴
      if 'Closing currently open stream...' in line:
        break
    if process:
      process.terminate()
      process.wait()
    ModelTwitchItem.process_done(self.streamlink_process_status[streamer_id])
    self.__clear_process_after_done(streamer_id)

  def __set_streamlink_info(self, streamer_id):
    '''
    set self.streamlink_info[streamer_id] about
    online, author, title, category, streams
    '''
    streams = self.__get_streams(streamer_id)
    if len(streams) < 1:
      return

    plugin = self.streamlink_plugins[streamer_id]

    author = plugin.get_author() or 'No Author'
    title = plugin.get_title() or 'No Title'
    category = plugin.get_category() or 'No Category'

    # 트위치 웹에서 띄어쓰기가 없거나 하나인데, 여기서는 여러개로 표시되는 문제
    title = title.strip() if type(title) == type('') else 'No Title'
    title = re.sub(r'(\s)+', r'\1', title)

    self.__set_streamlink_process_status(
      streamer_id,
      ['online', 'author', 'title', 'category', 'streams'],
      [len(streams) > 0, author, title, category, streams]
    )


  def __get_streams(self, streamer_id):
    streamlink_options = P.ModelSetting.get_list('streamlink_options', '|')
    command = ['python3', '-m', 'streamlink', '--json', '--force-progress'] + streamlink_options
    command += [f'{self.twitch_prefix}{streamer_id}']
    import subprocess
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    outs, errs = proc.communicate()
    outs = json.loads(outs) if outs is not None else ''
    errs = json.loads(errs) if errs is not None else ''
    streams = {}
    proc.kill()
    if 'streams' in outs:
      for quality in outs['streams']:
        streams[quality] = outs['streams'][quality]['url']
    return streams


  def __replace_unavailable_characters(self, source):
    replace_list = {
      ':': '∶',
      '/': '_',
      '\\': '_',
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


  def __parse_title_string(self, streamer_id, format_str):
    '''
    keywords: {author}, {title}, {category}, {streamer_id}
    and time foramt keywords: %m,%d,%Y, %H,%M,%S, ...
    https://docs.python.org/ko/3/library/datetime.html#strftime-and-strptime-format-codes
    '''
    result = format_str
    result = result.replace('{streamer_id}', streamer_id)
    result = result.replace('{author}', self.streamlink_process_status[streamer_id]['author'])
    result = result.replace('{title}', self.streamlink_process_status[streamer_id]['title'])
    result = result.replace('{category}', self.streamlink_process_status[streamer_id]['category'])
    result = datetime.now().strftime(result)
    return result


  def __init_properties(self, streamer_id):
    if streamer_id not in self.streamlink_plugins:
      self.streamlink_plugins[streamer_id] = None
    if streamer_id not in self.streamlink_processes:
      self.streamlink_processes[streamer_id] = None
    if streamer_id not in self.streamlink_process_status:
      self.streamlink_process_status[streamer_id] = {}
      self.__set_default_streamlink_process_status(streamer_id)


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
  download_path = db.Column(db.String)
  file_size = db.Column(db.String)
  elapsed_time = db.Column(db.String)
  quality = db.Column(db.String)
  options = db.Column(db.String)
  logs = db.Column(db.String)


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
  def get_file_by_id(cls, id):
    return cls.get_by_id(id).download_path

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
    db.session.query(cls).update({'running': False})
    db.session.commit()
  
  @classmethod
  def process_done(cls, streamlink_process_status_streamer_id):
    cls.update(streamlink_process_status_streamer_id)
    item = cls.get_by_id(streamlink_process_status_streamer_id['db_id'])
    item.running = False
    item.save()
  
  @classmethod
  def get_streamer_ids(cls):
    return [item.streamer_id for item in db.session.query(cls.streamer_id).distinct()]


  @classmethod
  def append(cls, streamer_id, streamlink_process_status):
    item = ModelTwitchItem()
    item.created_time = streamlink_process_status[streamer_id]['started_time']
    item.streamer_id = streamer_id
    item.author = streamlink_process_status[streamer_id]['author']
    item.title = streamlink_process_status[streamer_id]['title']
    item.category = streamlink_process_status[streamer_id]['category']
    item.download_path = streamlink_process_status[streamer_id]['download_path']
    item.file_size = streamlink_process_status[streamer_id]['size']
    item.elapsed_time = streamlink_process_status[streamer_id]['elapsed_time']
    item.quality = streamlink_process_status[streamer_id]['quality']
    item.options = '\n'.join(streamlink_process_status[streamer_id]['options'])
    item.logs = '\n'.join(streamlink_process_status[streamer_id]['logs'])
    item.save()
    return item.id

  @classmethod
  def update(cls, streamlink_process_status_streamer_id):
    item = cls.get_by_id(streamlink_process_status_streamer_id['db_id'])
    item.author = streamlink_process_status_streamer_id['author']
    item.title = streamlink_process_status_streamer_id['title']
    item.category = streamlink_process_status_streamer_id['category']
    item.quality = streamlink_process_status_streamer_id['quality']
    item.file_size = streamlink_process_status_streamer_id['size']
    item.elapsed_time = streamlink_process_status_streamer_id['elapsed_time']
    item.logs = '\n'.join(streamlink_process_status_streamer_id['logs'])
    item.save()