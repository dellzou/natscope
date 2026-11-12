# 第三方组件与许可声明

NatScope 自身以 **PolyForm Noncommercial License 1.0.0** 发布（见仓库根
`LICENSE`）。发布物（`NatScope.exe`，PyInstaller 单文件）内打包了下列第三方
组件，它们的许可**不因本项目的主许可而改变**，且其中若干条要求"二进制分发
时必须随附版权声明"——本文即为此而存在，请勿删除。

> 若你分发本软件的二进制副本，必须同时分发本文件与 `LICENSE`。

---

## 1. CPython（Python 解释器运行时）

- 版本：3.13.x
- 许可：PSF-2.0（Python Software Foundation License Agreement）
- 版权：Copyright © 2001-2026 Python Software Foundation. All rights reserved.
- 主页：<https://www.python.org/>
- 全文：<https://docs.python.org/3/license.html>

摘录（PSF 许可关键条款，原文）：

> PSF is making Python available to Licensee on an "AS IS" basis. PSF MAKES NO
> REPRESENTATIONS OR WARRANTIES, EXPRESS OR IMPLIED. BY WAY OF EXAMPLE, BUT
> NOT LIMITATION, PSF MAKES NO AND DISCLAIMS ANY REPRESENTATION OR WARRANTY OF
> MERCHANTABILITY OR FITNESS FOR ANY PARTICULAR PURPOSE OR THAT THE USE OF
> PYTHON WILL NOT INFRINGE ANY THIRD PARTY RIGHTS.
>
> PSF SHALL NOT BE LIABLE TO LICENSEE OR ANY OTHER USERS OF PYTHON FOR ANY
> INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES OR LOSS AS A RESULT OF
> MODIFYING, DISTRIBUTING, OR OTHERWISE USING PYTHON, OR ANY DERIVATIVE
> THEREOF, EVEN IF ADVISED OF THE POSSIBILITY THEREOF.

标准库中的 `zlib` 模块随附 zlib 的许可声明：Copyright © 1995-2024
Jean-loup Gailly and Mark Adler，条款为 zlib License（允许自由使用、修改与
再分发，要求保留源来源声明且不得以作者名义背书）。

## 2. PyInstaller（打包器）

- 版本：6.x
- 许可：GPL-2.0-or-later，**附 Bootloader 例外条款**
- 版权：Copyright © 2010-2026 PyInstaller Development Team
- 主页：<https://pyinstaller.org/>

说明：PyInstaller 的 bootloader（被嵌入 `NatScope.exe` 的那段 C 代码）以 GPL
发布，但带有官方的特殊例外，允许用它打包并分发**非自由许可**的应用程序，且
被分发的应用程序本身不因此变成 GPL。本项目的 `natscope.spec` 与 Python 源码
不属于 PyInstaller 的衍生作品。

> 例外条款摘要：bootloader 的 GPL 授权附带一项例外，允许将 bootloader 与
> 任意许可的应用程序捆绑并分发，且不要求该应用程序整体按 GPL 授权。原文见
> <https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt>。

## 3. Tcl/Tk（GUI 运行时，经 tkinter 使用）

- 版本：8.6.x（随 CPython 的 Windows 安装包提供）
- 许可：BSD 类（Tcl/Tk License，SPDX: `TCL`）
- 版权：Copyright © 1987-2026 The Regents of the University of California,
  Sun Microsystems, Inc., Scriptics Corporation, and other parties.

**本组件的许可明确要求：二进制分发时必须保留下列版权声明与许可声明。**
全文如下：

> This software is copyrighted by the Regents of the University of California,
> Sun Microsystems, Inc., Scriptics Corporation, and other parties. The
> following terms apply to all files associated with the software unless
> explicitly disclaimed in individual files.
>
> The authors hereby grant permission to use, copy, modify, distribute, and
> license this software and its documentation for any purpose, provided that
> existing copyright notices are retained in all copies and that this notice is
> included verbatim in any distributions. No written agreement, license, or
> royalty fee is required for any of the authorized uses. Modifications to this
> software may be copyrighted by their authors and need not follow the licensing
> terms described here, provided that the new terms are clearly indicated on the
> first page of each file where they apply.
>
> IN NO EVENT SHALL THE AUTHORS OR DISTRIBUTORS BE LIABLE TO ANY PARTY FOR
> DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES ARISING OUT OF
> THE USE OF THIS SOFTWARE, ITS DOCUMENTATION, OR ANY DERIVATIVES THEREOF, EVEN
> IF THE AUTHORS HAVE BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
>
> THE AUTHORS AND DISTRIBUTORS SPECIFICALLY DISCLAIM ANY WARRANTIES, INCLUDING,
> BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
> PARTICULAR PURPOSE, AND NON-INFRINGEMENT. THIS SOFTWARE IS PROVIDED ON AN "AS
> IS" BASIS, AND THE AUTHORS AND DISTRIBUTORS HAVE NO OBLIGATION TO PROVIDE
> MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR MODIFICATIONS.

## 4. Pillow（图标生成与报告出图，仅构建期与报告渲染使用）

- 版本：13.x（另见 `requirements.txt`）
- 许可：HPND（Historical Permission Notice and Disclaimer，SPDX: `HPND`）
- 版权：Copyright © 2010-2026 by Jeffrey A. Clark and contributors；
  Copyright © 1995-2011 by Secret Labs AB；Copyright © 1995 by Fredrik Lundh
  及 contributors。
- 主页：<https://python-pillow.org/>

全文如下：

> By obtaining, using, and/or copying this software and/or its associated
> documentation, you agree that you have read, understood, and will comply with
> the following terms and conditions:
>
> Permission to use, copy, modify and distribute this software and its
> documentation for any purpose and without fee is hereby granted, provided that
> the above copyright notice appears in all copies, and that both that copyright
> notice and this permission notice appear in supporting documentation, and that
> the name of Secret Labs AB or the author not be used in advertising or
> publicity pertaining to distribution of the software without specific,
> written prior permission.
>
> SECRET LABS AB AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS
> SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN
> NO EVENT SHALL SECRET LABS AB OR THE AUTHOR BE LIABLE FOR ANY SPECIAL,
> INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
> LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
> OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
> PERFORMANCE OF THIS SOFTWARE.

---

## 关于图标与品牌资产

`assets/icon.ico` 与 `assets/icon.png` 由 `tools/make_icons.py` 依据
`natscope/brand.py` 的几何参数**程序化生成**，不含任何第三方素材。

## 维护约定

- 升级 PyInstaller / Pillow 或内置的 Python 版本时，请同步更新本文的版本号与
  版权年份。
- 新增依赖前先确认其许可与本项目（非商业许可 + 允许二进制分发）兼容；GPL
  系依赖除非有官方例外条款，否则不得引入。
