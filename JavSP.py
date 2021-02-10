import os
import sys
import time
import logging
import requests
import threading

import colorama
import pretty_errors
from tqdm import tqdm


from core.datatype import ColoredFormatter


class TqdmOut:
    """用于将logging的stream输出重定向到tqdm"""
    @classmethod
    def write(cls, s, file=None, nolock=False):
        tqdm.write(s, file=file, end='', nolock=nolock)


pretty_errors.configure(display_link=True)

# 禁用导入的模块中的日志（仅对此时已执行导入模块的生效）
for i in logging.root.manager.loggerDict:
    logging.getLogger(i).disabled = True
# 配置 logging StreamHandler
root_logger = logging.getLogger()
console_handler = logging.StreamHandler(stream=TqdmOut)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(ColoredFormatter(fmt='%(message)s'))
root_logger.addHandler(console_handler)


from core.nfo import write_nfo
from core.file import select_folder, get_movies
from core.config import Config, cfg
from core.image import crop_poster
from core.datatype import Movie, MovieInfo
from web.base import download


CLEAR_LINE = '\r\x1b[K'


def import_crawlers(cfg: Config):
    """按配置文件的抓取器顺序将该字段转换为抓取器的函数列表"""
    unknown_mods = []
    for typ, cfg_str in cfg.Priority.items():
        mods = cfg_str.split(',')
        valid_mods = []
        for name in mods:
            try:
                # 导入fc2fan抓取器的前提: 配置了fc2fan的本地路径
                if name == 'fc2fan' and (not os.path.isdir(cfg.Crawler.fc2fan_local_path)):
                    logger.debug('由于未配置有效的fc2fan路径，已跳过该抓取器')
                    continue
                import_name = 'web.' + name
                __import__(import_name)
                valid_mods.append(import_name)  # 抓取器有效: 使用完整模块路径，便于程序实际使用
            except ModuleNotFoundError:
                unknown_mods.append(name)       # 抓取器无效: 仅使用模块名，便于显示
        cfg._sections['Priority'][typ] = tuple(valid_mods)
    if unknown_mods:
        logger.warning('配置的抓取器无效: ' + ', '.join(unknown_mods))


# 爬虫是IO密集型任务，可以通过多线程提升效率
def parallel_crawler(movie: Movie, tqdm_bar=None):
    """使用多线程抓取不同网站的数据"""
    def wrapper(parser, info: MovieInfo):
        """对抓取器函数进行包装，便于更新提示信息和自动重试"""
        crawler_name = threading.current_thread().name
        task_info = f'Crawler: {crawler_name}: {info.dvdid}'
        for retry in range(cfg.Network.retry):
            try:
                parser(info)
                logger.debug(f'{task_info}: 抓取成功')
                if isinstance(tqdm_bar, tqdm):
                    tqdm_bar.set_description(f'{crawler_name}: 抓取完成')
                break
            except requests.exceptions.RequestException as e:
                logger.debug(f'{task_info}: 网络错误，正在重试 ({retry+1}/{cfg.Network.retry}): \n{e}')
                if isinstance(tqdm_bar, tqdm):
                    tqdm_bar.set_description(f'{crawler_name}: 网络错误，正在重试')
            except Exception as e:
                logger.exception(f'{task_info}: 未处理的异常: {e}')

    # 根据影片的数据源获取对应的抓取器
    crawler_mods = cfg.Priority[movie.data_src]
    all_info = {i: MovieInfo(movie.dvdid) for i in crawler_mods}
    thread_pool = []
    for mod, info in all_info.items():
        parser = getattr(sys.modules[mod], 'parse_data')
        # 将all_info中的info实例传递给parser，parser抓取完成后，info实例的值已经完成更新
        th = threading.Thread(target=wrapper, name=mod, args=(parser, info))
        th.start()
        thread_pool.append(th)
    # 等待所有线程结束
    for th in thread_pool:
        th.join(timeout=(cfg.Network.retry * cfg.Network.timeout))
    return all_info


def info_summary(movie: Movie, all_info):
    """汇总多个来源的在线数据生成最终数据"""
    # 多线程下，all_info中的键值顺序不一定和爬虫的启动顺序一致，因此要重新获取优先级
    crawlers = cfg.Priority[movie.data_src]
    # 按照优先级取出各个爬虫获取到的信息
    final_info = MovieInfo(movie.dvdid)
    attrs = [i for i in dir(final_info) if not i.startswith('_')]
    for c in crawlers:
        absorbed = []
        crawlered_info = all_info[c]
        # 遍历所有属性，如果某一属性当前值为空而爬取的数据中含有该属性，则采用爬虫的属性
        for attr in attrs:
            current = getattr(final_info, attr)
            incoming = getattr(crawlered_info, attr)
            if (not current) and (incoming):
                setattr(final_info, attr, incoming)
                absorbed.append(attr)
        if absorbed:
            logger.debug(f"从'{c}'中获取了字段: " + ' '.join(absorbed))
    # 检查是否所有必需的字段都已经获得了值
    for attr in cfg.Crawler.required_keys:
        if not getattr(final_info, attr, None):
            logger.error(f"所有爬虫均未获取到字段: '{attr}'，抓取失败")
            return False
    # 必需字段均已获得了值：将最终的数据附加到movie
    movie.info = final_info
    return True


def generate_names(movie: Movie):
    """按照模板生成相关文件的文件名"""
    info = movie.info
    # 准备用来填充命名模板的字典
    d = {'num': info.dvdid}
    d['title'] = info.title if info.title else cfg.NamingRule.null_for_title
    if info.actress:
        d['actor'] = ','.join(info.actress)
    else:
        d['actor'] = cfg.NamingRule.null_for_actor
    remaining_keys = ['socre', 'serial', 'director', 'producer', 'publisher', 'publish_date']
    for i in remaining_keys:
        value = getattr(info, i, None)
        if value:
            d[i] = value
        else:
            d[i] = cfg.NamingRule.null_for_others
    # 生成相关文件的路径
    save_dir = os.path.normpath(cfg.NamingRule.save_dir.substitute(**d))
    basename = os.path.normpath(cfg.NamingRule.filename.substitute(**d))
    new_filepath = os.path.join(save_dir, basename + os.path.splitext(movie.files[0])[1])
    movie.save_dir = save_dir
    movie.nfo_file = os.path.join(save_dir, f'{basename}.nfo')
    movie.fanart_file = os.path.join(save_dir, f'{basename}-fanart.jpg')
    movie.poster_file = os.path.join(save_dir, f'{basename}-poster.jpg')
    setattr(movie, 'new_filepath', new_filepath)


if __name__ == "__main__":
    colorama.init(autoreset=True)
    logger = logging.getLogger('main')
    # 如果未配置有效代理，则显示相应提示
    if not cfg.Network.proxy:
        logger.warning('未配置有效代理，程序仍然会努力继续运行，但是部分功能可能受限：\n'
                       ' - 将尝试自动获取部分站点的免代理地址，但没有免代理地址的站点抓取器将无法工作\n'
                       ' - 抓取fanza的数据时，有一小部分影片仅能在日本归属的IP下抓取到')
    # 总的来说，不需要出现在日志里的显示信息，就直接使用tqdm.write；否则就使用logger.xxx
    tqdm.write('请选择要整理的文件夹：', end='')
    root = select_folder()
    if not root:
        tqdm.write('')  # 换行显示下面的错误信息
        logger.error('未选择文件夹，脚本退出')
        os.system('pause')
        os._exit(1)
    else:
        logger.info(f"{CLEAR_LINE}整理文件夹：'{root}'")
    # 导入抓取器，必须在chdir之前
    import_crawlers(cfg)
    os.chdir(root)

    tqdm.write(f'扫描影片文件...', end='')
    all_movies = get_movies(root)
    movie_count = len(all_movies)
    if movie_count == 0:
        logger.info('未找到影片文件，脚本退出')
        os.system('pause')
        os._exit(0)
    logger.info(f'{CLEAR_LINE}扫描影片文件：共找到 {movie_count} 部影片')
    tqdm.write('')

    outer_bar = tqdm(all_movies, desc='整理影片', ascii=True, leave=False)
    for movie in outer_bar:
        filenames = [os.path.split(i)[1] for i in movie.files]
        logger.info('正在整理: ' + ', '.join(filenames))
        inner_bar = tqdm(total=6, desc='步骤', ascii=True, leave=False)
        # 执行具体的抓取和整理任务
        inner_bar.set_description(f'启动并发任务')
        all_info = parallel_crawler(movie, inner_bar)
        inner_bar.update()

        inner_bar.set_description('汇总数据')
        has_required_keys = info_summary(movie, all_info)
        inner_bar.update()
        if has_required_keys:
            inner_bar.set_description('移动影片文件')
            generate_names(movie)
            os.makedirs(movie.save_dir)
            os.rename(movie.files[0], movie.new_filepath)
            inner_bar.update()

            inner_bar.set_description('下载封面图片')
            download(movie.info.cover, movie.fanart_file)
            inner_bar.update()

            inner_bar.set_description('裁剪海报封面')
            crop_poster(movie.fanart_file, movie.poster_file)
            inner_bar.update()

            inner_bar.set_description('写入NFO')
            write_nfo(movie.info, movie.nfo_file)
            inner_bar.update()

            logger.info(f'整理完成，相关文件已保存到: {movie.save_dir}\n')
        else:
            logger.error('整理失败\n')
        inner_bar.close()