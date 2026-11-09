// OA 代填面板独立打包入口。
//
// 面板源码 = backend/app/web/oa_panel.js（书签版同一个文件，单一来源）。
// 它是立即执行的 IIFE，动态 import 本文件时创建/显示面板——因此必须与
// 主 content script 分开打包（主包静态引入会在每个页面自动弹面板）。
// eslint-disable-next-line import/no-relative-packages
import '../../../backend/app/web/oa_panel.js';
