from doc_manager import doc_manager

FILE_PATH = r"C:\Users\lostar\Downloads\宋涛-政治经济学.pdf"

# 新增入库（自带幂等，重复跑不会重复入库）
res = doc_manager.add_document(FILE_PATH,"宋涛-政治经济学.pdf")
print(res)

# 删除文档
# doc_manager.delete_document("doc_id_xxx")

# 更新文档
# doc_manager.update_document(FILE_PATH,"宋涛-政治经济学.pdf")

# 列出全部文档
# docs = doc_manager.list_documents()
# print(docs)
