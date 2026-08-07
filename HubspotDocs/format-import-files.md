ormat import files
Last updated: August 6, 2026

Available with any of the following subscriptions, except where noted:

All products and plans

Additional subscriptions required for certain features
Before you import data into HubSpot, confirm that your files meet the required format and technical limits. Some limits are subscription dependent. Understand which file types are supported and how to structure and format property values, and how many rows and files you can import in a given period.

Prior to importing, review the get started with imports article. 


Technical requirements and limits
All files imported into HubSpot must:
Be a .csv, .xlsx, or .xls file.
Contain only one sheet.
Include a header row in which each column header corresponds to a property in HubSpot. The column headers can be organized in any order without affecting the import. You can confirm if a default property already exists to match your header or create a custom property prior to importing. Learn more about property requirements.
column-headers

Contain fewer than 1,000 columns.
Be UTF-8 encoded if the file includes foreign language characters.
If you're importing an Excel file:
Contain data that stays under Excel's worksheet and workbook maximum limits.
Contain cells in Number format when importing date and time picker properties.
Additional technical limits apply to the import tool depending on your HubSpot subscription. These limits include the size and row limits of an import file, as well as how many files and rows you can import per day.

If you're using HubSpot's free tools, you can:
Import files up to 20MB.
Complete up to 50 imports per day.
Import up to 500,000 rows per day. Files containing more than 500,000 rows, will take multiple days to complete.
Start up to 3 imports simultaneously. Up to 2 of these imports can be greater than 10,000 rows. If you start 4 or more simultaneous imports, they will be queued. 
If your account has a Smart CRM or a Starter, Professional, or Enterprise subscription, you can:
Import files up to 512 MB.
Complete up to 500 imports per day.
Import up to 10,000,000 rows per day, with a limit of 1,048,576 rows per file. If you're importing via the imports API, you can import up to 80,000,000 rows per day.
Start up to 3 imports simultaneously. Up to 2 of these imports can be greater than 10,000 rows. If you start 4 or more simultaneous imports, they will be queued.
These limits apply to a rolling 24-hour period and do not reset at a specific time of day. Learn more about your HubSpot subscription and its limitations. 

Format property values
For each row of your import file, you can add values for the property columns. The formatting of a value depends on the property field type and goal of the import.

Format values based on property field type
Depending on the property field type, there are specific formatting requirements:

Field Type	Requirements
Date picker

Date and time picker

Months can be a number, three letters, or a full name (10, OCT, Oct, OCTOBER, October), years can be two digits or four (2023, 23), and separators can be a forward slash, hyphen, or period (10/28/2023, 10-28-2023, 10.28.2023).

Valid date formats are day month year (e.g., 28/10/2023), month day year (e.g., 10-28-23), or year month day (e.g., 2023.OCT.28). 

If you've set validation rules, your imported values must follow the rules or they will not be imported.
Date and time picker

Timestamps can be added as hh:mm (28/10/2020 14:30) in all file types or as a UNIX timestamp in milliseconds in CSV files or an Excel file with cells in the Text number format. If you don't include a timestamp, the time is set to midnight by default.

Time zone offsets relative to your account's time zone can be added as +hh:mm or -hh:mm (e.g., 02-28-26 7:00AM +01:00).

Duration

For properties containing a duration, such as the Call duration property, your values must be formatted as a UNIX timestamp in milliseconds. Learn more about how to format timestamp values and how to convert a date into UNIX format.

Enumeration

The values in your import file must match either the label or the internal value of the property's defined options. For HubSpot default enumeration properties (e.g., Lifecycle stage, Industry), the values must match the internal value or the label in English.

File

To add existing files to a file property, include the File ID as your value. You can find a file's ID in the General section of a file's details.

Multiple checkboxes

To import multiple values, add a semicolon between each value without spaces (value 1;value 2). These property options must be created prior to the import.

To add or append values, add a semicolon before the first value (;value 3;value 4). This will append the value instead of overwriting the existing property value. 

Learn more about importing multiple checkbox values.

Number

Format the value based on the type of number property created:

Currency: for properties containing an amount of currency or a price, you must use one of HubSpot's accepted currencies. The list of accepted currencies and their currency codes can be found in the Currency tab of your account default settings. For USD, currency data should be formatted using decimals (e.g. 123.45)
Percentage: format your values with either a % percent sign or as a decimal. For example, to import 25%, your cell should contain either 25% or .25.
Phone number: the contact properties Phone number and Mobile phone number, to import and automatically format the phone number based on country code, format as +[country code][number]. If there is an extension, add ext[number]. For example, a phone number with a United States country code would look like +11234567890 ext123.
If you've set validation rules, your imported values must follow the rules or they will not be imported.
Single checkbox

Create a column for the single checkbox field and set the value on each line to True or Yes to display as Yes or checked in HubSpot or False or No to display as No or not checked in HubSpot.

Term properties

The line item property Term should be added in ISO-8601 period formats of PnYnMnD or PnW. For example, P12M or P24M for annual terms. If left blank, default terms will be set.

Text

If you've set validation rules, your imported values must follow the rules or they will not be imported.

User (owner)

To assign an owner to a record or activity during the import:
For objects, include an [Object] owner header. For activities, include an Activity assigned to header.
Add the user's name, email address, or the internal value of the owner property to each row in that column. If there is more than one user with the same name in your account, it is recommended to use the email address or the internal value of the Owner property instead. 
Users who are assigned as owners via import will not receive a notification that they were assigned a new record/activity.
Additional contact emails or company domains
If you're importing contacts with more than one email address, include an Additional email addresses column with their secondary emails. If you're importing companies with more than one domain name, include an Additional domains column with their secondary domains. To include multiple emails or domains in a cell, separate the values with semi-colons (e.g., email@email.com; emailtwo@email.com).

If you're importing to create contacts or companies, you cannot set a new contact's Email or new company's Company domain name to an email or domain that's an existing additional email/domain for another record.

Blank cells
The import tool ignores blank cells in a spreadsheet, so when importing new property data, leave cells blank for records without a value for the property. If an existing record already has a value for the property within HubSpot, blank cells will not clear the existing property value. To clear existing property values in bulk, you can manually edit the values or use the Edit records workflow action .
Deal collaborators
If you're adding collaborators to a deal via import, include a Deal collaborator header. For each cell, you must include two or more users, separated by semi-colons (e.g., user1@example.com;user2@example.com).
Product term and billing frequencies
If importing a Term property value, you can format the value in the Term column as a number in months (e.g., 10 for 10 months), as PXM where X is the number of months (e.g., P6M, for a term of 6 months) or PXY where X is the number of years (e.g., P1Y, for a term of 1 year).

If importing a Billing frequency property value, use monthly, annually, or quarterly if the product has a recurring price type. Leave the cell blank if the product has a one-time price.

Include required properties
Depending on which objects or activities you're importing, the following properties are required to create records, and must be included as columns headers in your files. If you're updating records, you must include a unique identifier (e.g., Record ID, Email, Company domain name).

While not required, you can import additional properties for each object, including custom and default properties. Refer to the default properties for contacts, companies, deals, tickets, calls, emails, meetings, notes, and tasks. You can also view all properties and their values in your property settings.
Contacts: Email. You can also include First name and Last name for basic identifying information.
Companies: at least one of Name or Company domain name.
Please note: the Company domain name property only accepts values up to the top-level domain (e.g., .com, .edu). Values with strings after it (e.g., .edu/home) will be automatically removed when you import. 
Deals: if you're creating new deals, Deal name, Pipeline, and Deal stage. Pipeline and Deal stage must match existing options in your account and the Deal stage must be a valid stage in that pipeline.
Tickets: if you're creating new tickets, Ticket name, Pipeline, and Ticket status.
Appointments: Appointment Start. The appointment start should be formatted as a date-time property.
Courses: Course Name.
Listings: Name.
Services: Name, Pipeline, and Pipeline Stage.
Products: if you're updating products, Record ID or SKU.
If you're updating products in an account with one currency, Unit price and Name.
If you're updating products in an account with multiple currencies, Price [Currency abbreviation] (e.g., Price USD) and Name.
Orders: Name, Pipeline, and Pipeline Stage.
Carts: Name.
Line items: Name, Quantity, Price, and the associated deals' Record ID or Deal name. Include the product's Product ID if you're also associating the line item with a product, which will be mapped as a line item property during the import.
Please note: when importing line items with deals, the import will not update the deal amount. To update the associated deal amount, you can manually edit the line items or associate the line items with a deal in HubSpot.
Calls: Call notes. When importing new calls, it's also recommended to include Activity date.
Emails: Email body and Email direction.
Meetings: Meeting description, Meeting start time, and Meeting end time. When importing new meetings, it's also recommended to include Activity date. The start time, end time, and activity date values should be formatted as date-time properties.
Notes: Note body.
Tasks: Task title and Due date. The due date should be formatted as a date-time property.
Please note: while not required, it's recommended to include Activity date when importing new activities to specify the date and time an activity occurred. If you don't include this property, the Activity date values are automatically set to the date and time of the import.
Requirements to update existing records or avoid duplicates
To update existing records and avoid duplicate records, your files must include a unique identifier property for each object. For all objects, you can export existing records and use the Record ID as a unique identifier, or use a custom property that requires unique values. For contacts, you can also use Email. For companies, you can also use Company domain name.

If you're importing multiple objects and are including Record IDs, it is recommended to differentiate the file column headers to match the ID with the correct object (e.g., one column called Record ID - Contacts and another called Record ID - Companies).

Please note: the following behavior applies if Email or Company domain name are included in your file:

If you're using a custom unique value property to deduplicate contacts, the Email property will still require unique values.
If you're using a custom unique value property to deduplicate companies, the Company domain name property will not require unique values. This means you can import duplicate company domains. If you don’t want multiple companies with same domain, you should remove duplicate domains from your file before importing, or use Company domain name as your unique identifier instead.
You can use a secondary email as the unique identifier for existing contacts who have a secondary email address listed in HubSpot. If you use a secondary email, and do not include the Record ID column in your file, the secondary email will not replace the primary email. However, if you include both the secondary email and Record ID as columns in your file, the secondary email will replace the primary email.
Requirements to associate records
Import associations in one file
In one file, you can associate two or more records per row. Include information about associated records and/or activities in the same row and include a unique identifier for each object. To associate one record with multiple activities or records of another object, include the record's unique identifier in multiple rows for each record you want to associate.

For example, Luke Danes is a manager at Luke's Diner, but a contractor at The Dragonfly Inn. To associate him with both companies, you'd need to include two rows for Luke Danes with the columns Email (or Record ID), and Company domain name (or Record ID) for each company.

Please note: if you don't include unique identifiers (e.g., Email, Company domain name, Record ID), the import will create duplicate records instead of associating each to the same record.
flex-associations-import-example

If you're importing same object associations, there are additional requirements.

Import associations in two files
To associate records of two objects in two files, use a common column to connect the records in each file. You can refer to the example files for more help importing and associating records.

Same object associations
You can import one file to associate records of the same object in bulk. During the import process, you'll need to select a checkbox to include same object associations.

When setting up your file, include information about the associated records in the same row and include a unique identifier for each record. The following additional requirements are specific to same object imports:

At least one record in a record pair must already exist in HubSpot before importing. For example, you can import a new contact and associate it with an existing contact, but you cannot import to create and associate two new contacts.
When associating existing contacts/companies, you can only use the primary email/company domain as your unique identifier. Secondary emails or domains are not supported.
If you're associating multiple records with one record, separate the records' unique identifier values by a semi-colon in the same cell (e.g., luke@lukesdiner.com; rory@yale.edu; sookie@dragonfly.com).
In the example file below, Rory and Jackson are new contacts being associated with unique labels to existing contacts, using their Email values as the unique identifier. Sookie is an existing contact being associated to two existing contacts with the same label. In the first row, the association label is part of a label pair, which means once the import is completed, the associated record will have the label Parent and Rory will have the paired label, Child.

associate-contacts-file
Parent/child associations
To create child-parent company associations via import, learn more about importing child companies.

Custom association labels
Subscription required A Professional or Enterprise subscription is required to use custom association labels.

To import association labels, include an Association label column. This column should only be added if you want to add labels to describe associations. If you don't need to label associations, the column is not required.

You must create the association labels in HubSpot prior to importing.
To set a company association as primary, include the value Primary in the Association label column for that row.
To set multiple labels to describe the relationship between two records, you can include multiple association label values in one cell, separated by a semicolon (e.g., Manager; Billing contact).
If you're importing association labels in a multiple file import, you need to include the Association label column and a unique identifier for the object you're associating in the same file.
If you're importing paired labels:
For cross-object associations, only include one of the labels in the Association label column. The labels will be assigned to the correct object during the import process.
For same object associations, the record in the Associated [unique identifier] column will receive the label added in the import process or included in the file's label column. The other record in the row will receive the other label in the pair.
Example files
At the beginning of the import process, you can generate an example file with the required properties for the selected objects and activities. If you'd like to review more examples with additional properties, refer to this article with sample files.

Next steps
Now that your import file is complete, follow the appropriate import directions: