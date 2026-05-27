 

# Software Requirements Specification (SRS)

**Project:** AI-Powered Procurement Agent for Procucev (WhatsApp Integration)  
**Date:** June 16, 2025

 

### **1\. Introduction**

**1.1 Purpose**  
This SRS defines the requirements for designing and implementing an AI-powered Procurement Agent integrated with WhatsApp, enabling registered users to interact via natural language for product queries and RFQ generation. The solution will enhance procurement engagement, speed of response, and operational efficiency for Procucev’s digital procurement platforms.

**1.2 Scope**  
The system will:

* Integrate with WhatsApp for conversational user interaction.  
* Connect to Procucev’s GMT and BFS platforms for real-time data.  
* Use AI to understand and respond to nuanced product queries.  
* Allow structured RFQ submission.  
* Support contextual understanding of vague or incomplete queries.

**1.3 Stakeholders**

* Procucev Product Team  
* Procucev IT/Engineering  
* End Users (Buyers/Vendors)  
* Vendors/Suppliers

### **2\. User Journey and Description**

**2.1 Background**

Procucev is a leader in enterprise procurement, providing platforms like Get My quoTe (GMT) and Buy From Stock (BFS) for vendor sourcing and inventory management. The AI Procurement Agent will serve as a digital assistant, available 24/7, to deliver fast, contextual, and actionable responses via WhatsApp.

**2.2 Product Perspective**

The AI Procurement Agent will be a new module, interfacing with existing Procucev backend systems and databases, and leveraging AI for natural language understanding.

**2.3 Assumptions and Dependencies**

* WhatsApp Business API account will be set up by the client.  
* Access to GMT and BFS APIs for product data.  
* OpenAI (or similar) API access for AI/LLM tasks.  
* Hosting on Azure. 

**2.4 User Journey**

**1\. User Registration & Authentication**

**Journey Steps:**

* The buyer sends a WhatsApp message to ProcuBot.  
* AI Agent checks registration status via API.  
  * If registered: Buyer can proceed.  
  * If not registered: Registration request is created via API; AI Agent informs the user that the Procucev team will contact them.

**Notes**: System Components: Authentication Module, WhatsApp Business API.

**2\. Buyer Inquiry**

**Journey Steps:**

* The buyer asks: “Do you have industrial motors, less than 3 years old, in Chennai?”  
* AI Agent parses the query, fetches data from BFS (Buy From Stock).  
* Responds with an accurate, contextual answer (availability, price, location, delivery).  
* Recommended vendors will have the 2 categories for the buyer : from  Own DB, from ProcucevDB (which will exclude the ones that are common with OwnDB)  
* If not available, suggest alternatives (e.g., nearby locations).  
* Buyer can continue with further inquiries or be nudged to create an RFQ.

**Notes**: Ensure the fallback/alternative suggestion logic is robust (e.g., suggesting nearby locations or similar products), as this enhances the value of the AI Agent.

**3\. Buyer RFQ Generation**

**Journey Steps:**

* AI Agent prompts for all required RFQ fields (item, spec, UOM, etc.), using any context already available.  
* Buyer provides information; AI Agent validates each response.  
* On completion, AI Agent creates the RFQ via API and responds with RFQ details.

**Notes**: Ensure the errors or incomplete information are handled (e.g., what happens if the user abandons the process or provides invalid data repeatedly).

### 3\. Functional Requirements

**3.1 Natural Language Interaction**

* **FR1:** The system shall accept and process user queries sent via WhatsApp.  
* **FR2:** The system shall interpret queries in natural language, including incomplete or vague queries.

**3.2 Product Query Response**

* **FR3:** The system shall provide from available database information on:  
  * Product availability  
  * Price  
  * Location  
  * Asset age  
  * Delivery timelines

**3.3 RFQ Generation**

* **FR4:** The system shall enable users to submit structured RFQs, capturing:  
  * Item Description  
  * Specification  
  * UOM (Unit of Measure)  
  * Category  
  * Quantity  
  * Location  
  * Age of Asset  
  * Buy Price

**3.4 Contextual Resolution**

* **FR5:** The system shall use contextual and historical data to clarify and resolve ambiguous queries.

**3.5 Authentication**

* **FR6:** The system shall authenticate users via WhatsApp registration and optionally via OTP.

**3.6 Lead Management**

* **FR7:** The system shall structure, validate, and submit RFQs or inquiries to the internal system.

**3.7 Offlines and Syncing Processes \-**  Any offlines or synching processes – which shall add new vendors/buyers, updated items being available for buy from stock need to be synched 

### 4\. Non-Functional Requirements

**4.1 Performance**

* **NFR1:** The system shall respond to user queries within 5 seconds.  
* **NFR2:** The system shall support concurrent sessions for at least 1,000 users.

**4.2 Security**

* **NFR3:** All data exchanges must be encrypted (HTTPS/TLS).  
* **NFR4:** User authentication and authorization must be enforced.

**4.3 Reliability**

* **NFR5:** The system shall be available 99.5% of the time.

**4.4 Scalability**

* **NFR6:** The system shall be scalable to support future increases in user and data volume.


### **5\. System Architecture**

**5.1 Components**

| Component | Description |
| :---- | :---- |
| WhatsApp Business API | Handles WhatsApp messaging. Integration via Twilio/WATI/ICS. |
| RAG Module | Retrieval-Augmented Generation: Ingests product/supplier data into vector DB for AI retrieval. |
| AI Platform | OpenAI GPT (or similar) for language understanding and response generation. |
| Product Database Integration | REST API connection to GMT/BFS for real-time data. |
| RFQ/Inquiry Module | Structures and manages RFQ submission and tracking. |
| Authentication Module | Validates users via WhatsApp and OTP. |
| Offline Sync Module | Periodically ingests new supplier/product data into RAG module. |
| Database | PostgreSQL/MongoDB for session and message logs. |

**5.2 Technology Stack**

* **Frontend:** WhatsApp (via Twilio, WATI, or ICS)  
* **Middleware:** Python (FastAPI/Serverless)  
* **AI Platform:** OpenAI GPT (API access)  
* **Backend Integration:** REST APIs (GMT/BFS)  
* **Database:** PostgreSQL / MongoDB  
* **Hosting:** Azure

 

### **6\. External Interfaces**

**6.1 WhatsApp Business API**

* Integration with WhatsApp provider.  
* Handles inbound/outbound messaging.

**6.2 Procucev APIs (GMT/BFS)**

* REST APIs for product and supplier data.

**6.3 AI/LLM API**

* API access for natural language understanding and response generation.

 

### **7\. Data Flow**

1. User sends a query via WhatsApp.  
2. Message received by WhatsApp API provider.  
3. Middleware processes message, authenticates user.  
4. AI module interprets query, retrieves data (from vector DB or backend).  
5. Response generated and sent back to user via WhatsApp.  
6. If RFQ, structured data is validated and submitted to backend.

 

### **8\. Implementation Considerations**

* **User onboarding:** WhatsApp registration, optional OTP.  
* **Data privacy:** Ensure compliance with data protection regulations.  
* **Logging:** All interactions logged for audit and improvement.  
* **Error handling:** Graceful handling of API failures and user errors.

 

### **9\. Acceptance Criteria**

* Users can successfully query product details and receive accurate responses.  
* Users can generate and submit RFQs via WhatsApp.  
* The system responds within defined SLAs.  
* All interactions are secure and logged.

 

### **10\. Appendix**

* **Glossary:**  
  * **RFQ:** Request for Quotation  
  * **RAG:** Retrieval-Augmented Generation  
  * **GMT:** Get My quoTe  
  * **BFS:** Buy From Stock