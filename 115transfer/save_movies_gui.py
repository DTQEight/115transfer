import tkinter as tk
from tkinter import ttk, messagebox
import pandas as pd
import os
from datetime import datetime
import pyperclip

EXCEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'movies_data.xlsx')

class MovieApp:
    def __init__(self, root):
        self.root = root
        self.root.title("电影磁力链接保存程序")
        self.root.geometry("1000x700")
        self.root.state('zoomed')
        
        # 分页变量（按电影的页码字段分页）
        self.current_page_num = 1  # 当前显示的页码值
        self.all_page_nums = []  # 所有唯一的页码值列表
        
        # 创建主框架
        main_frame = ttk.Frame(root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # 搜索区域
        search_frame = ttk.Frame(main_frame)
        search_frame.grid(row=0, column=0, columnspan=5, sticky=(tk.W, tk.E), pady=5)
        
        ttk.Label(search_frame, text="搜索:").grid(row=0, column=0, padx=5)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=50)
        self.search_entry.grid(row=0, column=1, padx=5, sticky=(tk.W, tk.E))
        self.search_entry.bind('<KeyRelease>', self.search_movies)
        
        self.search_btn = ttk.Button(search_frame, text="搜索电影名", command=self.search_movies)
        self.search_btn.grid(row=0, column=2, padx=5)
        
        self.clear_search_btn = ttk.Button(search_frame, text="清除搜索", command=self.clear_search)
        self.clear_search_btn.grid(row=0, column=3, padx=5)
        
        self.search_result_var = tk.StringVar()
        self.search_result_label = ttk.Label(search_frame, textvariable=self.search_result_var)
        self.search_result_label.grid(row=0, column=4, padx=10, sticky=(tk.W, tk.E))
        
        search_frame.columnconfigure(1, weight=1)
        
        # 输入区域
        input_frame = ttk.LabelFrame(main_frame, text="添加电影", padding="20")
        input_frame.grid(row=1, column=0, columnspan=4, sticky=(tk.W, tk.E), pady=10)
        
        # 页码
        ttk.Label(input_frame, text="页码:").grid(row=0, column=0, sticky=tk.E, pady=8, padx=10)
        self.page_var = tk.StringVar()
        self.page_entry = ttk.Entry(input_frame, textvariable=self.page_var, width=40)
        self.page_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=10, pady=8)
        
        # 电影名
        ttk.Label(input_frame, text="电影名:").grid(row=1, column=0, sticky=tk.E, pady=8, padx=10)
        self.movie_var = tk.StringVar()
        self.movie_entry = ttk.Entry(input_frame, textvariable=self.movie_var, width=40)
        self.movie_entry.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=10, pady=8)
        
        # 磁力链接
        ttk.Label(input_frame, text="磁力链接:").grid(row=2, column=0, sticky=tk.E, pady=8, padx=10)
        self.magnet_var = tk.StringVar()
        self.magnet_entry = ttk.Entry(input_frame, textvariable=self.magnet_var, width=40)
        self.magnet_entry.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=10, pady=8)
        
        # 按钮区域
        button_frame = ttk.Frame(input_frame)
        button_frame.grid(row=3, column=0, columnspan=2, pady=12)
        button_frame.grid_columnconfigure(0, weight=1)
        button_frame.grid_columnconfigure(1, weight=1)
        button_frame.grid_columnconfigure(2, weight=1)
        
        self.add_btn = ttk.Button(button_frame, text="添加电影", command=self.add_movie)
        self.add_btn.grid(row=0, column=0, padx=5)
        
        self.update_btn = ttk.Button(button_frame, text="更新选中", command=self.update_movie)
        self.update_btn.grid(row=0, column=1, padx=5)
        
        self.delete_btn = ttk.Button(button_frame, text="删除选中", command=self.delete_movie)
        self.delete_btn.grid(row=0, column=2, padx=5)
        
        self.clear_btn = ttk.Button(button_frame, text="清空输入", command=self.clear_inputs)
        self.clear_btn.grid(row=0, column=3, padx=5)
        
        # 表格区域
        table_frame = ttk.LabelFrame(main_frame, text="电影列表 (双击磁力链接复制)", padding="15")
        table_frame.grid(row=2, column=0, columnspan=4, sticky=(tk.W, tk.E, tk.N, tk.S), pady=10)
        
        # 分页控制区域（按电影页码分页）
        page_control_frame = ttk.Frame(table_frame)
        page_control_frame.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=10)
        
        # 跳转到页码 - 左对齐
        go_frame = ttk.Frame(page_control_frame)
        go_frame.grid(row=0, column=0, sticky=tk.W)
        ttk.Label(go_frame, text="跳转到页码:").grid(row=0, column=0, padx=5)
        self.go_to_page_var = tk.StringVar()
        self.go_to_page_entry = ttk.Entry(go_frame, textvariable=self.go_to_page_var, width=10)
        self.go_to_page_entry.grid(row=0, column=1, padx=5)
        self.go_to_page_btn = ttk.Button(go_frame, text="跳转", command=self.go_to_page)
        self.go_to_page_btn.grid(row=0, column=2, padx=5)
        
        # 页码导航 - 居中
        nav_frame = ttk.Frame(page_control_frame)
        nav_frame.grid(row=0, column=1, sticky=(tk.W, tk.E))
        nav_frame.grid_columnconfigure(0, weight=1)
        
        nav_center = ttk.Frame(nav_frame)
        nav_center.grid(row=0, column=0)
        
        ttk.Label(nav_center, text="当前页码:").grid(row=0, column=0, padx=5)
        self.current_page_var = tk.StringVar()
        self.current_page_var.set("1")
        self.current_page_label = ttk.Label(nav_center, textvariable=self.current_page_var, font=('Arial', 11, 'bold'))
        self.current_page_label.grid(row=0, column=1, padx=2)
        
        ttk.Separator(nav_center, orient=tk.VERTICAL).grid(row=0, column=2, padx=15, sticky=(tk.N, tk.S))
        
        self.first_btn = ttk.Button(nav_center, text="<<", command=self.first_page, width=5)
        self.first_btn.grid(row=0, column=3, padx=2)
        
        self.prev_btn = ttk.Button(nav_center, text="<", command=self.prev_page, width=5)
        self.prev_btn.grid(row=0, column=4, padx=2)
        
        self.next_btn = ttk.Button(nav_center, text=">", command=self.next_page, width=5)
        self.next_btn.grid(row=0, column=5, padx=2)
        
        self.last_btn = ttk.Button(nav_center, text=">>", command=self.last_page, width=5)
        self.last_btn.grid(row=0, column=6, padx=2)
        
        ttk.Separator(nav_center, orient=tk.VERTICAL).grid(row=0, column=7, padx=15, sticky=(tk.N, tk.S))
        
        ttk.Label(nav_center, text="共").grid(row=0, column=8, padx=5)
        self.total_pages_label = ttk.Label(nav_center, text="0", font=('Arial', 11, 'bold'))
        self.total_pages_label.grid(row=0, column=9, padx=2)
        ttk.Label(nav_center, text="个页码").grid(row=0, column=10, padx=5)
        
        page_control_frame.grid_columnconfigure(0, weight=1)
        page_control_frame.grid_columnconfigure(1, weight=3)
        
        # 创建 Treeview
        columns = ('序号', '页码', '电影名', '磁力链接', '保存时间')
        self.tree = ttk.Treeview(table_frame, columns=columns, show='headings', height=12)
        
        for col in columns:
            self.tree.heading(col, text=col, anchor=tk.CENTER)
            if col == '磁力链接':
                self.tree.column(col, width=300, anchor=tk.W)
            else:
                self.tree.column(col, width=120, anchor=tk.CENTER)
        
        # 滚动条
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.grid(row=1, column=1, sticky=(tk.N, tk.S))
        
        # 创建红色标签样式（用于空磁力链接）
        self.tree.tag_configure('empty_magnet', foreground='red')
        
        # 绑定双击事件
        self.tree.bind('<Double-1>', self.on_double_click)
        
        # 配置拉伸
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)
        input_frame.columnconfigure(1, weight=1)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(1, weight=1)
        
        # 加载数据
        self.load_data()
    
    def load_movies(self):
        """加载电影数据"""
        if os.path.exists(EXCEL_FILE):
            try:
                df = pd.read_excel(EXCEL_FILE)
                return df
            except:
                return pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间'])
        return pd.DataFrame(columns=['序号', '页码', '电影名', '磁力链接', '保存时间'])
    
    def save_movies(self, df):
        """保存电影数据"""
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
    
    def add_movie(self):
        """添加电影"""
        page = self.page_var.get().strip()
        movie_name = self.movie_var.get().strip()
        magnet_link = self.magnet_var.get().strip()
        
        if not all([page, movie_name]):
            messagebox.showwarning("警告", "页码和电影名不能为空！")
            return
        
        df = self.load_movies()
        
        new_movie = {
            '序号': len(df) + 1,
            '页码': page,
            '电影名': movie_name,
            '磁力链接': magnet_link,
            '保存时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        df = pd.concat([df, pd.DataFrame([new_movie])], ignore_index=True)
        df['序号'] = range(1, len(df) + 1)
        self.save_movies(df)
        
        self.load_data()
        self.clear_inputs()
        messagebox.showinfo("成功", f"已添加：《{movie_name}》\n共保存了 {len(df)} 部电影")
    
    def update_movie(self):
        """更新电影"""
        selected = self.tree.selection()
        
        if not selected:
            messagebox.showwarning("警告", "请先在表格中选择要更新的电影！")
            return
        
        item = self.tree.item(selected[0])
        values = item['values']
        
        index = int(values[0])
        old_name = values[2]
        
        # 获取新值
        page = self.page_var.get().strip()
        movie_name = self.movie_var.get().strip()
        magnet_link = self.magnet_var.get().strip()
        
        if not any([page, movie_name, magnet_link]):
            messagebox.showwarning("警告", "请输入要更新的内容！\n（页码、电影名或磁力链接）")
            return
        
        df = self.load_movies()
        
        if page:
            df.loc[df['序号'] == index, '页码'] = int(page)
        if movie_name:
            df.loc[df['序号'] == index, '电影名'] = movie_name
        if magnet_link:
            df.loc[df['序号'] == index, '磁力链接'] = magnet_link
        
        df.loc[df['序号'] == index, '保存时间'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        self.save_movies(df)
        self.load_data()
        self.clear_inputs()
        messagebox.showinfo("成功", f"已更新：《{old_name}》")
    
    def delete_movie(self):
        """删除电影"""
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("警告", "请先选择要删除的电影！")
            return
        
        item = self.tree.item(selected[0])
        values = item['values']
        index = values[0]
        movie_name = values[2]
        
        if messagebox.askyesno("确认", f"确定要删除《{movie_name}》吗？"):
            df = self.load_movies()
            df = df[df['序号'] != index]
            df['序号'] = range(1, len(df) + 1)
            self.save_movies(df)
            self.load_data()
            messagebox.showinfo("成功", "删除成功！")
    
    def clear_inputs(self):
        """清空输入"""
        self.page_var.set("")
        self.movie_var.set("")
        self.magnet_var.set("")
    
    def go_to_page(self):
        """跳转到指定页码"""
        page_input = self.go_to_page_var.get().strip()
        if page_input:
            try:
                target_page = int(page_input)
                if target_page in self.all_page_nums:
                    self.current_page_num = target_page
                    self.load_data()
                else:
                    messagebox.showwarning("警告", f"页码 {target_page} 不存在！")
            except ValueError:
                messagebox.showwarning("警告", "请输入有效的页码数字！")
    
    def first_page(self):
        """第一页"""
        if self.all_page_nums:
            self.current_page_num = self.all_page_nums[0]
            self.load_data()
    
    def prev_page(self):
        """上一页"""
        if self.all_page_nums:
            idx = self.all_page_nums.index(self.current_page_num)
            if idx > 0:
                self.current_page_num = self.all_page_nums[idx - 1]
                self.load_data()
    
    def next_page(self):
        """下一页"""
        if self.all_page_nums:
            idx = self.all_page_nums.index(self.current_page_num)
            if idx < len(self.all_page_nums) - 1:
                self.current_page_num = self.all_page_nums[idx + 1]
                self.load_data()
    
    def last_page(self):
        """最后一页"""
        if self.all_page_nums:
            self.current_page_num = self.all_page_nums[-1]
            self.load_data()
    
    def load_data(self):
        """加载表格数据（按页码显示）"""
        # 清空现有数据
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        df = self.load_movies()
        
        if df.empty:
            self.all_page_nums = []
            self.current_page_var.set("0")
            self.total_pages_label.config(text="0")
            return
        
        # 获取所有唯一的页码，按降序排列
        self.all_page_nums = sorted(df['页码'].unique(), reverse=True)
        
        # 如果当前页码不在列表中，使用第一个页码
        if self.current_page_num not in self.all_page_nums:
            self.current_page_num = self.all_page_nums[0]
        
        # 筛选当前页码的电影
        page_df = df[df['页码'] == self.current_page_num]
        
        # 显示数据
        if not page_df.empty:
            for _, row in page_df.iterrows():
                magnet = row['磁力链接']
                # 检查磁力链接是否为空或空白
                if pd.isna(magnet) or str(magnet).strip() == '':
                    magnet_display = '(空)'
                    tags = ('empty_magnet',)
                else:
                    magnet_display = magnet
                    tags = ()
                
                self.tree.insert('', tk.END, values=(
                    row['序号'],
                    row['页码'],
                    row['电影名'],
                    magnet_display,
                    row['保存时间']
                ), tags=tags)
        
        # 更新页码显示
        self.current_page_var.set(str(self.current_page_num))
        self.total_pages_label.config(text=str(len(self.all_page_nums)))
    
    def search_movies(self, event=None):
        """搜索电影"""
        search_text = self.search_var.get().strip().lower()
        
        if not search_text:
            self.clear_search()
            return
        
        df = self.load_movies()
        
        if df.empty:
            return
        
        mask = df['电影名'].str.lower().str.contains(search_text, na=False)
        result_df = df[mask]
        
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        if not result_df.empty:
            for _, row in result_df.iterrows():
                magnet = row['磁力链接']
                if pd.isna(magnet) or str(magnet).strip() == '':
                    magnet_display = '(空)'
                    tags = ('empty_magnet',)
                else:
                    magnet_display = magnet
                    tags = ()
                
                self.tree.insert('', tk.END, values=(
                    row['序号'],
                    row['页码'],
                    row['电影名'],
                    magnet_display,
                    row['保存时间']
                ), tags=tags)
            self.search_result_var.set(f"找到 {len(result_df)} 条记录")
        else:
            self.search_result_var.set("未找到匹配的电影")
        
        self.current_page_var.set("搜索")
        self._set_page_buttons_state('disabled')
    
    def clear_search(self):
        """清除搜索"""
        self.search_var.set("")
        self.search_result_var.set("")
        self._set_page_buttons_state('normal')
        self.load_data()
    
    def _set_page_buttons_state(self, state):
        """设置分页按钮状态"""
        for btn in [self.first_btn, self.prev_btn, self.next_btn, self.last_btn, self.go_to_page_btn]:
            btn.configure(state=state)
    
    def on_double_click(self, event):
        """双击复制磁力链接"""
        region = self.tree.identify("region", event.x, event.y)
        if region == "cell":
            column = self.tree.identify_column(event.x)
            item = self.tree.identify_row(event.y)
            
            if column == '#4':
                values = self.tree.item(item, 'values')
                if values:
                    magnet_link = values[3]
                    pyperclip.copy(magnet_link)
                    messagebox.showinfo("复制成功", f"已复制磁力链接到剪贴板:\n{magnet_link}")

def main():
    root = tk.Tk()
    app = MovieApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()
