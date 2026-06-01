import pandas as pd
import os
from datetime import datetime

EXCEL_FILE = 'movies_data.xlsx'

def load_movies():
    """加载已保存的电影数据"""
    if os.path.exists(EXCEL_FILE):
        try:
            df = pd.read_excel(EXCEL_FILE)
            return df
        except:
            return pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间'])
    return pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间'])

def save_movies(df):
    """保存电影数据到 Excel 文件"""
    df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')

def add_movie():
    """添加一部电影"""
    print("\n=== 添加电影信息 ===")
    
    page = input("请输入页码：").strip()
    movie_name = input("请输入电影名：").strip()
    magnet_link = input("请输入磁力链接：").strip()
    
    if not all([page, movie_name, magnet_link]):
        print("错误：所有字段都不能为空！")
        return False
    
    df = load_movies()
    
    new_index = len(df) + 1
    new_movie = {
        '序号': new_index,
        '页码': page,
        '电影名': movie_name,
        '磁力链接': magnet_link,
        '保存时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
    df['序号'] = range(1, len(df) + 1)
    save_movies(df)
    
    print(f"成功保存：《{movie_name}》")
    print(f"当前共保存了 {len(df)} 部电影")
    return True

def view_movies():
    """查看所有已保存的电影"""
    df = load_movies()
    
    if df.empty:
        print("\n暂无保存的电影记录")
        return
    
    print(f"\n=== 已保存的电影 ({len(df)}部) ===")
    print(df.to_string(index=False))

def delete_movie():
    """删除电影记录"""
    df = load_movies()
    
    if df.empty:
        print("\n暂无保存的电影记录")
        return
    
    view_movies()
    
    try:
        index = int(input("\n请输入要删除的电影序号："))
        if 1 <= index <= len(df):
            removed = df[df['序号'] == index]
            df = df[df['序号'] != index]
            df['序号'] = range(1, len(df) + 1)
            save_movies(df)
            print(f"已删除：《{removed['电影名'].values[0]}》")
        else:
            print("无效的序号！")
    except ValueError:
        print("请输入有效的数字！")
    except Exception as e:
        print(f"删除失败：{e}")

def update_movie():
    """更新电影记录"""
    df = load_movies()
    
    if df.empty:
        print("\n暂无保存的电影记录")
        return
    
    view_movies()
    
    try:
        index = int(input("\n请输入要更新的电影序号："))
        if 1 <= index <= len(df):
            print("\n请输入新信息（直接回车保持不变）：")
            
            movie_row = df[df['序号'] == index].iloc[0]
            
            page = input(f"页码 [{movie_row['页码']}]: ").strip()
            movie_name = input(f"电影名 [{movie_row['电影名']}]: ").strip()
            magnet_link = input(f"磁力链接 [{movie_row['磁力链接']}]: ").strip()
            
            if page:
                df.loc[df['序号'] == index, '页码'] = page
            if movie_name:
                df.loc[df['序号'] == index, '电影名'] = movie_name
            if magnet_link:
                df.loc[df['序号'] == index, '磁力链接'] = magnet_link
            
            df.loc[df['序号'] == index, '保存时间'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            save_movies(df)
            print(f"已更新：《{df.loc[df['序号'] == index, '电影名'].values[0]}》")
        else:
            print("无效的序号！")
    except ValueError:
        print("请输入有效的数字！")
    except Exception as e:
        print(f"更新失败：{e}")

def main():
    """主函数"""
    print("=" * 50)
    print("电影磁力链接保存程序（Excel 版）")
    print("=" * 50)
    
    while True:
        print("\n请选择操作：")
        print("1. 添加电影")
        print("2. 查看所有电影")
        print("3. 删除电影")
        print("4. 更新电影")
        print("5. 退出程序")
        
        choice = input("\n请输入选项 (1-5): ").strip()
        
        if choice == '1':
            add_movie()
        elif choice == '2':
            view_movies()
        elif choice == '3':
            delete_movie()
        elif choice == '4':
            update_movie()
        elif choice == '5':
            print("\n再见！")
            break
        else:
            print("无效的选项，请重新输入！")

if __name__ == '__main__':
    main()
