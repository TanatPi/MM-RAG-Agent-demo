# rag_agent.py

import os
import asyncio
import json
import requests

from typing import Annotated

from semantic_kernel import Kernel
from semantic_kernel.functions import kernel_function
from semantic_kernel.functions.kernel_arguments import KernelArguments
from semantic_kernel.agents.chat_completion.chat_completion_agent import ChatCompletionAgent
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion, AzureChatPromptExecutionSettings

from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential


# === ENV ===
deployment = "gpt-4o-mini"
subscription_key = os.environ.get("AZURE_OPENAI_KEY")
endpoint = os.environ.get("AZURE_OPENAI_RESOURCE")
embedding_endpoint = os.environ.get('AZURE_OPENAI_EMBEDDING_MODEL_RESOURCE')
headers = {
    "Content-Type": "application/json",
    "Authorization": os.environ.get('AZURE_OPENAI_EMBEDDING_MODEL_RESOURCE_KEY')
}
search_endpoint = os.environ.get('COG_SEARCH_ENDPOINT')
admin_key = os.environ.get('COG_SEARCH_ADMIN_KEY')




# === Search Plugin ===
class SearchPlugin:
    def __init__(self, text_index_name = "pdf-economic-summary", table_index_name = "pdf-economic-summary-tables", image_index_name="pdf-economic-summary-images"):
        self.search_client_text = SearchClient(
            endpoint=search_endpoint,
            index_name=text_index_name,
            credential=AzureKeyCredential(admin_key)
        )
        self.search_client_table = SearchClient(
            endpoint=search_endpoint,
            index_name=table_index_name,
            credential=AzureKeyCredential(admin_key)
        )
        self.search_client_image = SearchClient(
            endpoint=search_endpoint,
            index_name=image_index_name,
            credential=AzureKeyCredential(admin_key)
        )
        self.embedding_endpoint = embedding_endpoint
        self.headers = headers

    async def get_embedding(self, text):
        def sync_post():
            response = requests.post(
                url=self.embedding_endpoint,
                headers=self.headers,
                json={"input": text}
            )
            response.raise_for_status()
            return response.json()["data"][0]["embedding"]
        return await asyncio.to_thread(sync_post)

    async def _search(self, query, client, select, top_k=10, filter=None):
        vector = await self.get_embedding(query)
        vector_query = VectorizedQuery(vector=vector, k_nearest_neighbors=top_k, fields="contentVector")
        results = client.search(
            search_text=query,
            vector_queries=[vector_query],
            select=select,
            top=top_k,
            filter=filter,
        )
        return json.dumps([
            {
                "page": doc.get("page", "N/A"),
                "filename": doc.get("doc_name", "unknown.txt"),
                "content": doc.get("content", ""),
                "table": doc.get("table", "N/A"),
                "figure": doc.get("figure", "N/A")
            } for doc in results
        ], ensure_ascii=False, indent=2)

    @kernel_function(description="Search document text content")
    async def search_text_content(self, query: Annotated[str, "User query"], filter=None, top_k=10) -> Annotated[str, "Search results"]:
        return await self._search(query, self.search_client_text, select=["content", "page", "doc_name"], filter=filter, top_k=top_k)

    @kernel_function(description="Search table data")
    async def search_table_content(self, query: Annotated[str, "User query"], filter=None, top_k=10) -> Annotated[str, "Search results"]:
        return await self._search(query, self.search_client_table, select=["content", "page", "table", "doc_name"], filter=filter, top_k=top_k)

    @kernel_function(description="Search image data")
    async def search_image_content(self, query: Annotated[str, "User query"], filter=None, top_k=10) -> Annotated[str, "Search results"]:
        return await self._search(query, self.search_client_image, select=["content", "page", "figure", "doc_name"], filter=filter, top_k=top_k)


# === System Prompt ===
system_prompt_RAG = f"""You are a helpful and professional female financial assistant. Answer only based on the provided information (no external knowledge or assumptions).

                Instructions:
                - Use clear, simple Thai (or English) that is easy for Thai native speakers to understand.
                - Focus on **concise**, **structured**, and **informative** answers — avoid greetings, filler, or redundant phrases.
                - Use bullet points for multiple facts or arguments.
                - Do not omit any numbers or quantitative details.
                - Always **cite the page number** (e.g., "หน้า 6") and if applicable, **table number** (e.g., "ตารางที่ 2").
                - If information is from both **image** and **text**, include both if they support the answer.
                - Refer to the document using the name in the `filename` field.
                - Treat "อเมริกา", "สหรัฐฯ", and "สหรัฐ" as equivalent.
                - Do **not** mention figure numbers.
                - Summarize all relevant information across multiple pages into one coherent answer.

                Output should be clean and directly parsable by another agent — avoid extra commentary or follow-up phrases like "Do you have anything else to ask?"

                Example Output:
                จากหน้า 6 ตารางที่ 4 และรูปภาพในหน้า 11 ของ monthly-summary.pdf เศรษฐกิจไทยมีแนวโน้มเข้าสู่ภาวะถดถอย เนื่องจาก:
                - เศรษฐกิจไทยไตรมาส 1/2025 เติบโต 3.1% YoY โดยได้แรงหนุนจากการส่งออกที่เพิ่มขึ้น 13.8% จากการเร่งนำเข้าของคู่ค้าก่อนสหรัฐขึ้นภาษี
                - ความต้องการในประเทศยังเปราะบาง และการท่องเที่ยวเริ่มชะลอตัว
                - รายได้บริษัทจดทะเบียนคาดว่าจะเติบโต 20% แต่มีความเสี่ยงจากเศรษฐกิจชะลอ
                - ดัชนี SET เผชิญแรงกดดันจากเศรษฐกิจและการเมือง แต่ระดับปัจจุบันอยู่ในช่วงต่ำสุดเทียบเท่าช่วงโควิด
                
                Reference: หน้า 6 ตารางที่ 4, หน้า 11–12 ของ monthly-summary.pdf
            """

# === Agent & Plugin Constructor ===
def get_mm_rag_agent():
    kernel = Kernel()
    kernel.add_service(
        AzureChatCompletion(
            service_id="rag_chat_service",
            deployment_name="gpt-4o-mini",
            api_key=subscription_key,
            endpoint=endpoint,
        )
    )
    settings = AzureChatPromptExecutionSettings(
        service_id="rag_chat_service",
        temperature=0.4,
        top_p=1.0,
        frequency_penalty=0.0,
        presence_penalty=0.0,
    )
    agent = ChatCompletionAgent(
        kernel=kernel,
        arguments=KernelArguments(settings=settings),
        name="searchservivce-mm-rag-agent",
        instructions=system_prompt_RAG,
    )
    return agent

def get_search_plugin(text_index_name = "pdf-economic-summary", table_index_name = "pdf-economic-summary-tables", image_index_name="pdf-economic-summary-images"):
    return SearchPlugin(text_index_name = text_index_name, table_index_name = table_index_name, image_index_name=image_index_name)
