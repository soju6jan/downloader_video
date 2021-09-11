import streamlink
from time import time

url = 'https://www.twitch.tv/kimdoe'


session = streamlink.Streamlink()
session.set_plugin_option("twitch", "disable-ads", True)
session.set_plugin_option("twitch", "disable-hosting", True)
session.set_plugin_option("twitch", "disable-reruns", True)
# session.set_plugin_option("twitch", "low-latency", True)
session.set_option('hls-live-edge', 1)
session.set_option('ringbuffer-size', 16 * 1024 * 1024)


twitch_object = session.resolve_url(url)
'''
  twitch_object.get_author(),
  twitch_object.get_title(),
  twitch_object.get_category(), 
'''
# print(
#   twitch_object.get_author(),
#   twitch_object.get_title(),
#   twitch_object.get_category(), 
# )

streams = twitch_object.streams()
for i in streams:
  print(streams[i].url)
best_stream = streams['best']
# print(
#   best_stream.url,
#   best_stream.args
# )
stream_object = best_stream.open()

# chunk_size = 8192 # bytes
chunk_size = 128 # bytes
current_size = 0
part_size = 20 * 1024 * 1024
size_limit = 100 * 1024 * 1024 # 100 mega bytes
mid_chunk_size = 1 * 1024 * 1024
last_chunk_size = 128

filename = 'test'
idx = 1
before_stream_bytes = b''

start = time()
while current_size < size_limit:
  with open(filename + str(idx) + '.mp4', 'wb') as f: 
    print(f'write file: {filename + str(idx) + ".mp4"}')
    while True:
      stream_bytes = stream_object.read(chunk_size)
      f.write(stream_bytes)
      current_size += chunk_size
      if current_size > (idx * part_size):
        break
    f.close()
    idx += 1
end = time()

print(
  f'chunk size: {chunk_size}. elapsed time: {end - start}s'
)
