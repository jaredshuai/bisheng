import time
import os
import yaml
from typing import Any, Dict, Tuple, Type, Union
from loguru import logger
from langchain_core.tools import BaseTool, Tool
from langchain_core.pydantic_v1 import BaseModel, Extra, Field, root_validator
from langchain_core.language_models.base import LanguageModelLike
from langchain.chains.question_answering import load_qa_chain
from bisheng_langchain.retrievers import EnsembleRetriever
from bisheng_langchain.vectorstores import ElasticKeywordsSearch, Milvus
from bisheng_langchain.rag.init_retrievers import (
    BaselineVectorRetriever,
    KeywordRetriever,
    MixRetriever,
    SmallerChunksVectorRetriever,
)
from bisheng_langchain.rag.utils import import_by_type, import_class


class MultArgsSchemaTool(Tool):

    def _to_args_and_kwargs(self, tool_input: Union[str, Dict]) -> Tuple[Tuple, Dict]:
        # For backwards compatibility, if run_input is a string,
        # pass as a positional argument.
        if isinstance(tool_input, str):
            return (tool_input, ), {}
        else:
            return (), tool_input


class BishengRAGTool():

    def __init__(
        self,
        vector_store: Milvus,
        keyword_store: ElasticKeywordsSearch,
        llm: LanguageModelLike,
        collection_name: str,
        **kwargs
    ) -> None:
        self.vector_store = vector_store
        self.keyword_store = keyword_store
        self.llm = llm
        self.collection_name = collection_name
        
        yaml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config/baseline_v2.yaml')
        with open(yaml_path, 'r') as f:
            self.params = yaml.safe_load(f)
        
        # init retriever
        retriever_list = []
        retrievers = self.params['retriever']['retrievers']
        for retriever in retrievers:
            retriever_type = retriever.pop('type')
            retriever_params = {
                'vector_store': self.vector_store,
                'keyword_store': self.keyword_store,
                'splitter_kwargs': retriever['splitter'],
                'retrieval_kwargs': retriever['retrieval'],
            }
            retriever_list.append(self._post_init_retriever(retriever_type=retriever_type, **retriever_params))
        self.retriever = EnsembleRetriever(retrievers=retriever_list)

        # init qa chain    
        if 'prompt_type' in self.params['generate']:
            prompt_type = self.params['generate']['prompt_type']
            prompt = import_class(f'bisheng_langchain.rag.prompts.{prompt_type}')
        else:
            prompt = None
        self.qa_chain = load_qa_chain(
            llm=self.llm, 
            chain_type=self.params['generate']['chain_type'], 
            prompt=prompt, 
            verbose=False
        )
    
    def _post_init_retriever(self, retriever_type, **kwargs):
        retriever_classes = {
            'KeywordRetriever': KeywordRetriever,
            'BaselineVectorRetriever': BaselineVectorRetriever,
            'MixRetriever': MixRetriever,
            'SmallerChunksVectorRetriever': SmallerChunksVectorRetriever,
        }
        if retriever_type not in retriever_classes:
            raise ValueError(f'Unknown retriever type: {retriever_type}')

        input_kwargs = {}
        splitter_params = kwargs.pop('splitter_kwargs')
        for key, value in splitter_params.items():
            splitter_obj = import_by_type(_type='textsplitters', name=value.pop('type'))
            input_kwargs[key] = splitter_obj(**value)

        retrieval_params = kwargs.pop('retrieval_kwargs')
        for key, value in retrieval_params.items():
            input_kwargs[key] = value

        input_kwargs['vector_store'] = kwargs.pop('vector_store')
        input_kwargs['keyword_store'] = kwargs.pop('keyword_store')

        retriever_class = retriever_classes[retriever_type]
        return retriever_class(**input_kwargs)
    
    def retrieval_and_rerank(self, query):
        """
        retrieval and rerank
        """
        # EnsembleRetriever直接检索召回会默认去重
        docs = self.retriever.get_relevant_documents(
            query=query, 
            collection_name=self.collection_name
        )
        logger.info(f'retrieval docs origin: {len(docs)}')

        # delete redundancy according to max_content 
        doc_num, doc_content_sum = 0, 0
        for doc in docs:
            doc_content_sum += len(doc.page_content)
            if doc_content_sum > self.params['generate']['max_content']:
                break
            doc_num += 1
        docs = docs[:doc_num]
        logger.info(f'retrieval docs after delete redundancy: {len(docs)}')

        # 按照文档的source和chunk_index排序，保证上下文的连贯性和一致性
        if self.params['post_retrieval'].get('sort_by_source_and_index', False):
            logger.info('sort chunks by source and chunk_index')
            docs = sorted(docs, key=lambda x: (x.metadata['source'], x.metadata['chunk_index']))
        return docs
    
    def run(self, query) -> str:
        docs = self.retrieval_and_rerank(query)
        try:
            ans = self.qa_chain({"input_documents": docs, "question": query}, return_only_outputs=True)
        except Exception as e:
            logger.error(f'question: {question}\nerror: {e}')
            ans = {'output_text': str(e)}
        rag_answer = ans['output_text']
        return rag_answer
    
    async def arun(self, query: str) -> str:
        rag_answer = self.run(query)
        return rag_answer
    
    @classmethod
    def get_api_tool(cls, name, **kwargs: Any) -> BaseTool:
        class InputArgs(BaseModel):
            query: str = Field(description='questions to ask')

        function_description = kwargs.get('description','')
        kwargs.pop('description')
        return MultArgsSchemaTool(name=name,
                                  description=function_description,
                                  func=cls(**kwargs).run,
                                  coroutine=cls(**kwargs).arun,
                                  args_schema=InputArgs)


if __name__ == '__main__':
    from langchain.chat_models import ChatOpenAI
    from langchain.embeddings import OpenAIEmbeddings
    # embedding
    embeddings = OpenAIEmbeddings(model='text-embedding-ada-002')
    # llm
    llm = ChatOpenAI(model='gpt-4-1106-preview', temperature=0.01)

    # milvus
    vector_store = Milvus(
            embedding_function=embeddings,
            connection_args={
                "host": '110.16.193.170',
                "port": '50062',
            },
    )
    # es
    keyword_store = ElasticKeywordsSearch(
        index_name='default_es',
        elasticsearch_url='http://110.16.193.170:50062/es',
        ssl_verify={'basic_auth': ["elastic", "oSGL-zVvZ5P3Tm7qkDLC"]},
    )

    tool = BishengRAGTool.get_api_tool(
        name='rag_knowledge_retrieve', 
        vector_store=vector_store, 
        keyword_store=keyword_store, 
        llm=llm, 
        collection_name='rag_finance_report_0_benchmark_caibao_1000_source_title',
        description='金融年报财报知识库问答'
    )
    print(tool.run('能否根据2020年金宇生物技术股份有限公司的年报，给我简要介绍一下报告期内公司的社会责任工作情况？'))