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
    'twitch_filename_format': '[%Y%m%d][%H%M] [{category}] {title}',
    'twitch_directory_name_format': '{author} ({id})/%Y-%m',
    'twitch_streamer_ids': '',
    'twitch_auto_make_folder': 'True',
    'twitch_auto_start': 'False',
    'twitch_interval': '* * * * *',
    'streamlink_quality': 'best',
    'streamlink_options': '--force-progress\n--twitch-disable-hosting\n--twitch-disable-ads\n--twitch-disable-reruns\n',
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
    'online': bool,
    'author': str,
    'title': str,
    'category': str,
    'started_timestamp': 0,
    'quality': '',
    'options': [],
    'path': '',
    'log': ['message1', 'message2',],
    'status': 'status_string',
    'size': '',
    'elapsed_time': '',
    'speed': '',
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
      if sub == 'entity_list': # 처음에 queue에서 요청.
        return jsonify(self.streamlink_process_status)
      elif sub == 'web_list':
        return jsonify({})
      elif sub == 'install':
        LogicTwitch.install_streamlink()
        self.is_streamlink_installed = True
        return jsonify({})
      elif sub == 'db_remove':
        return jsonify(ModelTwitchItem.delete_by_id(req.form['id']))
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def setting_save_after(self):
    streamer_ids = P.ModelSetting.get_list('twitch_streamer_ids', '|') # | 또는 엔터
    for streamer_id in streamer_ids:
      self.__init_properties(streamer_id)


  def scheduler_function(self):
    try:
      if not self.streamlink_session:
        import streamlink
        self.streamlink_session = streamlink.Streamlink() 
      
      streamlink_quality = P.ModelSetting.get('streamlink_quality')
      streamlink_options = P.ModelSetting.get_list('streamlink_options', '|')
      streamer_ids = P.ModelSetting.get_list('twitch_streamer_ids', '|') # | 또는 엔터

      download_directory = P.ModelSetting.get('twitch_download_path')
      make_child_directory = P.ModelSetting.get_bool('twitch_auto_make_folder')
      child_directory_name_format = P.ModelSetting.get('twitch_directory_name_format')
      filename_format = P.ModelSetting.get('twitch_filename_format')

      self.streamlink_plugins = {
        streamer_id: self.streamlink_session.resolve_url(self.twitch_prefix + streamer_id) for streamer_id in streamer_ids
      }
      for streamer_id in self.streamlink_plugins:
        # check if running
        # 이거 set_info 뒤로 빼면 실시간으로 제목, 카테고리 갱신 될 것. 
        # 근데 안할거임. 트위치에 부담될 것 같음.
        # 그냥 실시간으로 다운 상황만 갱신할래
        if self.streamlink_processes[streamer_id]:
          continue
        
        self.__set_default_streamlink_process_status(streamer_id)
        self.__set_streamlink_info(streamer_id)

        if self.streamlink_process_status[streamer_id]['online']:
          # set filename
          download_path = os.path.abspath(download_directory)
          if make_child_directory:
            directory_name = self.__parse_title_string(streamer_id, child_directory_name_format)
            download_path = os.path.join(download_path, directory_name)
            if not os.path.isdir(download_path):
              os.makedirs(download_path, exist_ok=True) # mkdir -p
          filename = self.__parse_title_string(streamer_id, filename_format)
          download_path = os.path.join(download_path, filename)
          if not download_path.endswith('.mp4'):
            download_path = download_path + '.mp4'
          # set 
          self.__set_streamlink_process_status(
            streamer_id, 
            ['path', 'started_timestamp', 'options', 'quality'], 
            [download_path, int(datetime.now().strftime("%s%f"))/1000, streamlink_options, streamlink_quality]
          )
          # spawn child
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
    streamer_ids = P.ModelSetting.get_list('twitch_streamer_ids', '|')
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
    # TODO: socketio 보내는데 자꾸 에러남
    # 왜그러지
    try:
      self.socketio_callback('status', jsonify(self.streamlink_process_status))
    except Exception as e:
      logger.error(e)
      logger.error(traceback.format_exc())



  def __set_default_streamlink_process_status(self, streamer_id: str):
    keys = [
      'online', 'author', 'title', 
      'category', 'started_timestamp', 'quality', 
      'options', 'path', 'log', 
      'status', 'size', 'elapsed_time',
      'speed',
    ]
    values = [
      False, 'No Author', 'No Title', 
      'No Category', 0, '', 
      [], '', [], 
      '', '', '',
      '',
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
      command = ['python3', '-m', 'streamlink', '--output', filename]
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
      return f'spwan success: {streamer_id}'
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
      return f'spawn failed: {streamer_id}'

  """
  [cli][info] streamlink is running as root! Be careful!
  [cli][info] Found matching plugin twitch for URL https://www.twitch.tv/jungtaejune
  [cli][info] Available streams: audio_only, 160p (worst), 360p, 480p, 720p, 720p60, 1080p60 (best)
  [cli][info] Opening stream: 1080p60 (hls)
  [plugins.twitch][info] Will skip ad segments
  [download][ddol_210905_1530.mp4] Written 23.68 GB (6h43m50s @ 1005.8 KB/s)  
  """
  def __parse_download_log(self, log):
    # [download][...] filename] Written 23.68 GB (6h43m50s @ 1005.8 KB/s) 
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
    ''' TODO:
    다운로드 완료했을 때 정상적으로 정지하는지 확인 안해봤음. 
    '''
    for line in iter(process.stdout.readline, b''):
      line = str(line).strip()
      log_splitted = [i[1:] if i.startswith('[') else i.strip() for i in line.split(']')]
      if log_splitted[0] == 'download':
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
      elif len(line) > 0:
        keys = ['log']
        values = [self.streamlink_process_status[streamer_id]['log']+[line]]
        if 'Opening stream:' in line:
          quality = line.split()[-2]
          keys.append('quality')
          values.append(quality)
        self.__set_streamlink_process_status(
          streamer_id, 
          keys, 
          values
        )
    process.wait()
    self.streamlink_plugins[streamer_id] = None
    self.streamlink_processes[streamer_id] = None
    self.streamlink_process_status[streamer_id] = {}


  def __set_streamlink_info(self, streamer_id):
    info = {}
    plugin = self.streamlink_plugins[streamer_id]

    author = plugin.get_author() or 'No Author'
    title = plugin.get_title() or 'No Title'
    category = plugin.get_category() or 'No Category'

    # 웹에서 띄어쓰기가 없거나 하나인데 여러개로 표시되는 문제
    title = title.strip() if type(title) == type('') else 'No Title'
    title = re.sub(r'(\s)+', r'\1', title)

    # 파일명에 불가능한 문자. :, \, /, : * ? " < > |
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
      author = author.replace(key, replace_list[key])
      title = title.replace(key, replace_list[key])
      category = category.replace(key, replace_list[key])
    
    self.__set_streamlink_process_status(
      streamer_id, 
      ['online', 'author', 'title', 'category'], 
      [len(plugin.streams()) > 0, author, title, category]
    )
  

  def __parse_title_string(self, streamer_id, format_str):
    '''
    keywords: {author}, {title}, {category}, {id}
    and time foramt keywords: %m,%d,%Y, %H,%M,%S, ...
    https://docs.python.org/ko/3/library/datetime.html#strftime-and-strptime-format-codes
    '''
    result = format_str
    result = result.replace('{id}', streamer_id)
    result = result.replace('{author}', self.streamlink_process_status[streamer_id]['author'])
    result = result.replace('{title}', self.streamlink_process_status[streamer_id]['title'])
    result = result.replace('{category}', self.streamlink_process_status[streamer_id]['category'])
    result = datetime.now().strftime(result)
    return result


  def __init_properties(self, streamer_id, reset=False):
    if reset or streamer_id not in self.streamlink_plugins:
      self.streamlink_plugins[streamer_id] = None
    if reset or streamer_id not in self.streamlink_processes:
      self.streamlink_processes[streamer_id] = None
    if reset or streamer_id not in self.streamlink_process_status:
      self.streamlink_process_status[streamer_id] = {}
      self.__set_default_streamlink_process_status(streamer_id)


#########################################################
# db 
#########################################################
class ModelTwitchItem(db.Model):
  __tablename__ = '{package_name}_twitch_item'.format(package_name=P.package_name)
  __table_args__ = {'mysql_collate': 'utf8_general_ci'}
  __bind_key__ = P.package_name
  id = db.Column(db.Integer, primary_key=True)
  created_time = db.Column(db.DateTime)
  completed_time = db.Column(db.DateTime)
  streamer_name = db.Column(db.String)
  streamer_id = db.Column(db.String)
  title = db.Column(db.String)
  quality = db.Column(db.String)
  filepath = db.Column(db.String)
  filename = db.Column(db.String)
  savepath = db.Column(db.String)
  status = db.Column(db.String)

  def __init__(self):
    self.created_time = datetime.now()
  
  def __repr__(self):
    return repr(self.as_dict())
  
  def as_dict(self):
    ret = {x.name: getattr(self, x.name) for x in self.__table__.columns}
    ret['created_time'] = self.created_time.strftime('%Y-%m-%d %H:%M:%S') 
    ret['completed_time'] = self.completed_time.strftime('%Y-%m-%d %H:%M:%S') if self.completed_time is not None else None
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
    if search is not None and search != '':
      if search.find('|') != -1:
        tmp = search.split('|')
        conditions = []
        for tt in tmp:
          if tt != '':
            conditions.append(cls.filename.like('%'+tt.strip()+'%') )
        query = query.filter(or_(*conditions))
      elif search.find(',') != -1:
        tmp = search.split(',')
        for tt in tmp:
          if tt != '':
            query = query.filter(cls.filename.like('%'+tt.strip()+'%'))
      else:
        query = query.filter(cls.filename.like('%'+search+'%'))
    if option == 'completed':
      query = query.filter(cls.status == 'completed')

    query = query.order_by(desc(cls.id)) if order == 'desc' else query.order_by(cls.id)
    return query  

  @classmethod
  def get_list_incompleted(cls):
    return db.session.query(cls).filter(cls.status != 'completed').all()


