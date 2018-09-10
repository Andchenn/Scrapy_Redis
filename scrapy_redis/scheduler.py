import importlib

import six
from scrapy.utils.misc import load_object

from scrapy_redis import defaults, connection


class Scheduler(object):
    """
    Redis-based scheduler
    Settings
    --------
    SCHEDULER_PERSIST : bool (default: False)
        Whether to persist or clear redis queue.
    SCHEDULER_FLUSH_ON_START : bool (default: False)
        Whether to flush redis queue on start.
    SCHEDULER_IDLE_BEFORE_CLOSE : int (default: 0)
        How many seconds to wait before closing if no message is received.
    SCHEDULER_QUEUE_KEY : str
        Scheduler redis key.
    SCHEDULER_QUEUE_CLASS : str
        Scheduler queue class.
    SCHEDULER_DUPEFILTER_KEY : str
        Scheduler dupefilter redis key.
    SCHEDULER_DUPEFILTER_CLASS : str
        Scheduler dupefilter class.
    SCHEDULER_SERIALIZER : str
        Scheduler serializer.
    """

    def __init__(self, server,
                 persist=False,
                 flush_on_start=False,
                 queue_key=defaults.SCHEDULER_QUEUE_KEY,
                 queue_cls=defaults.SCHEDULER_QUEUE_CLASS,
                 dupefilter_key=defaults.SCHEDULER_DUPEFILTER_KEY,
                 dupefilter_cls=defaults.SCHEDULER_DUPEFILTER_CLASS,
                 idle_before_close=0,
                 serializer=None):
        """
        Initialize scheduler
        Parameters
        :param server:Redis
        The redis server instance
        :param persist:bool
        Whether to flush requests when closing.Default is False
        flush_on_start : bool
        Whether to flush requests on start. Default is False.
        :param queue_key:str
        Requests queue key
        :param queue_cls:str
        Importable path to the queue class
        :param dupefilter_key:str
        Duplicates filter key.
        :param dupefilter_cls:str
        Importable path to the dupefilter class
        :param idle_before_close:int
        Timeout before giving up.
        idle_before_close : int
        Timeout before giving up.
        """

        if idle_before_close < 0:
            raise TypeError("idle_before_close cannot be negative")

        self.server = server
        self.persist = persist
        self.flush_on_start = flush_on_start
        self.queue_key = queue_key
        self.queue_cls = queue_cls
        self.dupefilter_cls = dupefilter_cls
        self.dupefilter_key = dupefilter_key
        self.idle_before_close = idle_before_close
        self.serializer = serializer
        self.stats = None

    def __len__(self):
        return len(self.queue)

    @classmethod
    def from_settings(cls, settings):
        kwargs = {
            'persist': settings.getbool('SCHEDULER_PERSIST'),
            'flush_on_start': settings.getbool('SCHEDULER_FLUSH_ON_START'),
            'idle_before_close': settings.getint('SCHEDULER_IDLE_BEFORE_CLOSE'),
        }
        # 如果缺少这些值，则意味着我们要使用默认值。
        optional = {
            # TODO: Use custom prefixes for this settings to note that are
            # specific to scrapy-redis.
            'queue_key': 'SCHEDULER_QUEUE_KEY',
            'queue_cls': 'SCHEDULER_QUEUE_CLASS',
            'dupefilter_key': 'SCHEDULER_DUPEFILTER_KEY',
            # 我们使用默认设置名称来保持兼容性。
            'dupefilter_cls': 'DUPEFILTER_CLASS',
            'serializer': 'SCHEDULER_SERIALIZER',
        }
        for name, setting_name in optional.items():
            val = settings.get(setting_name)
            if val:
                kwargs[name] = val
        # 支持序列化程序作为模块的路径。
        if isinstance(kwargs.get('serializer'), six.string_types):
            kwargs['serializer'] = importlib.import_module(kwargs['serializer'])
        server = connection.from_settings(settings)
        # 确保连接正常。
        server.ping()
        return cls(server=server, **kwargs)

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls.from_settings(crawler.settings)
        # FIXME：目前，仅从此构造函数支持统计信息
        instance.stats = crawler.stats
        return instance

    def open(self, spider):
        self.spider = spider
        try:
            self.queue = load_object(self.queue_cls)(
                server=self.server,
                spider=spider,
                key=self.queue_key % {'spider': spider.name},
                serializer=self.serializer

            )
        except TypeError as e:
            raise ValueError("Failed to instantiate queue class '%s':%s", self.queue_cls, e)
        self.df = load_object(self.dupefilter_cls).from_spider(spider)

        if self.flush_on_start:
            self.flush()

        # tote如果队列中已有请求恢复爬网
        if len(self.queue):
            spider.log("Resuming crawl (%d requests scheduled)" % len(self.queue))

    def close(self, reason):
        if not self.persist:
            self.flush()

    def flush(self):
        self.df.clear()
        self.queue.clear()

    def enqueue_request(self, request):
        if not request.dont_filter and self.df.request_seen(request):
            self.df.log(request, self.spider)
            return False
        if self.stats:
            self.stats.inc_value('scheduler/enqueued/redis', spider=self.spider)
        self.queue.push(request)
        return True

    def next_request(self):
        block_pop_timeout = self.idle_before_close
        request = self.queue.pop(block_pop_timeout)
        if request and self.stats:
            self.stats.inc_value('scheduler/enqueued/redis', spider=self.spider)
        return request

    def has_pending_requests(self):
        return len(self) > 0
